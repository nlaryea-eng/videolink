#!/usr/bin/env python3
"""Focused regression tests for the VideoLink hotkey core."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import resolver_core

try:
    import videolink
    VIDEOLINK_IMPORT_ERROR = None
except ModuleNotFoundError as error:
    videolink = None
    VIDEOLINK_IMPORT_ERROR = error


class DependencyGuardTests(unittest.TestCase):
    """Fail — never skip — when the module under test cannot even import.

    Without this, running the suite with the wrong interpreter skips every
    test and still reports OK, which reads as green when nothing ran.
    """

    def test_videolink_module_imports(self) -> None:
        self.assertIsNone(
            VIDEOLINK_IMPORT_ERROR,
            f"videolink failed to import ({VIDEOLINK_IMPORT_ERROR}); "
            "run the suite with the project venv: .venv/bin/python -m unittest",
        )


def isolated_state_file() -> mock._patch:
    """Point the last-subtitle-window state file at a fresh empty directory.

    open_in_iina(ipc_path=...) consults this file and would otherwise read —
    and act on — real state left behind by actual subtitle sessions on the
    developer's machine.
    """
    state_dir = tempfile.mkdtemp(prefix="videolink-test-state-")
    return mock.patch.object(
        videolink, "LAST_SUBTITLE_WINDOW_FILE",
        os.path.join(state_dir, "last_iina_socket.txt"),
    )


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class VideoLinkCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        if videolink is None:
            self.skipTest(f"videolink dependencies unavailable: {VIDEOLINK_IMPORT_ERROR}")

    def test_ipc_launch_does_not_fall_back_to_plain_open(self) -> None:
        calls: list[list[str]] = []

        def fake_which(name: str) -> str | None:
            return "/usr/bin/open" if name == "open" else None

        def fake_run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with (
            mock.patch.object(videolink.sys, "platform", "darwin"),
            mock.patch.object(videolink.shutil, "which", side_effect=fake_which),
            mock.patch.object(videolink.os.path, "exists", return_value=False),
            mock.patch.object(videolink.subprocess, "run", side_effect=fake_run),
            isolated_state_file(),
        ):
            opened = videolink.open_in_iina("https://example.com/video.mp4", ipc_path="/tmp/vl.sock")

        self.assertFalse(opened)
        self.assertEqual(calls, [])

    def test_plain_launch_can_use_macos_open_fallback(self) -> None:
        calls: list[list[str]] = []

        def fake_which(name: str) -> str | None:
            return "/usr/bin/open" if name == "open" else None

        def fake_run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with (
            mock.patch.object(videolink.sys, "platform", "darwin"),
            mock.patch.object(videolink.shutil, "which", side_effect=fake_which),
            mock.patch.object(videolink.os.path, "exists", return_value=False),
            mock.patch.object(videolink.subprocess, "run", side_effect=fake_run),
            isolated_state_file(),
        ):
            opened = videolink.open_in_iina("https://example.com/video.mp4")

        self.assertTrue(opened)
        self.assertEqual(calls, [["open", "-a", "IINA", "https://example.com/video.mp4"]])

    def test_subtitle_service_url_is_normalized(self) -> None:
        args = videolink.parse_args([
            "--subtitle-service-url",
            "http://127.0.0.1:9999/",
            "https://example.com/article",
        ])
        self.assertEqual(args.subtitle_service_url, "http://127.0.0.1:9999")

    def test_direct_html_media_sources_cover_common_page_shapes(self) -> None:
        page = """
        <video src="/media/clip.mp4"></video>
        <meta property="og:video" content="https://cdn.example.com/live/master.m3u8?token=1">
        <script type="application/ld+json">
        {"@type": "VideoObject", "name": "Report", "contentUrl": "/dash/report.mpd"}
        </script>
        """
        videos = videolink.discover_html_media_sources(page, "https://news.example.com/story")
        urls = [url for _title, url in videos]

        self.assertIn("https://news.example.com/media/clip.mp4", urls)
        self.assertIn("https://cdn.example.com/live/master.m3u8?token=1", urls)
        self.assertIn("https://news.example.com/dash/report.mpd", urls)

    def test_bild_delivery_fallback_prefers_hls_urls(self) -> None:
        page = """
        "https:\\/\\/hds.ak.token.bild.de\\/video\\/manifest.mpd,delivery=mpd"
        "https:\\/\\/hds.ak.token.bild.de\\/video\\/master.m3u8,delivery=hls"
        """
        urls = videolink.discover_page_delivery_urls(page, "https://www.bild.de/video")
        self.assertEqual(urls, ["https://hds.ak.token.bild.de/video/master.m3u8,delivery=hls"])

    def test_create_live_session_sends_media_and_iina_socket(self) -> None:
        requests = []

        def fake_urlopen(request, timeout):
            requests.append((request, timeout))
            return FakeResponse({"id": "session-1", "ipc_socket_path": "/tmp/vl-test.sock"})

        with (
            mock.patch.object(videolink, "SUBTITLE_SERVICE_URL", "http://127.0.0.1:8765"),
            mock.patch.object(videolink, "make_iina_ipc_path", return_value="/tmp/vl-test.sock"),
            mock.patch.object(videolink, "urlopen", side_effect=fake_urlopen),
        ):
            session, error = videolink.create_live_subtitle_session(
                "https://cdn.example.com/video.m3u8",
                "Example",
                "auto",
                "small",
            )

        self.assertIsNone(error)
        self.assertEqual(session["id"], "session-1")
        request, timeout = requests[0]
        self.assertEqual(timeout, videolink.SESSION_CREATE_TIMEOUT_SECONDS)
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/sessions")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["media_url"], "https://cdn.example.com/video.m3u8")
        self.assertEqual(payload["iina_ipc_path"], "/tmp/vl-test.sock")
        self.assertEqual(payload["mode"], "live")

    def test_create_live_session_uses_supplied_iina_socket(self) -> None:
        requests = []

        def fake_urlopen(request, timeout):
            requests.append((request, timeout))
            return FakeResponse({"id": "session-1", "ipc_socket_path": "/tmp/vl-supplied.sock"})

        with (
            mock.patch.object(videolink, "SUBTITLE_SERVICE_URL", "http://127.0.0.1:8765"),
            mock.patch.object(
                videolink,
                "make_iina_ipc_path",
                side_effect=AssertionError("caller supplied the socket path"),
            ),
            mock.patch.object(videolink, "urlopen", side_effect=fake_urlopen),
        ):
            session, error = videolink.create_live_subtitle_session(
                "https://cdn.example.com/video.m3u8",
                "Example",
                "auto",
                "small",
                ipc_path="/tmp/vl-supplied.sock",
            )

        self.assertIsNone(error)
        self.assertEqual(session["id"], "session-1")
        payload = json.loads(requests[0][0].data.decode("utf-8"))
        self.assertEqual(payload["iina_ipc_path"], "/tmp/vl-supplied.sock")

    def test_start_live_session_reports_success(self) -> None:
        requests = []

        def fake_urlopen(request, timeout):
            requests.append((request, timeout))
            return FakeResponse({"state": "starting"})

        with (
            mock.patch.object(videolink, "SUBTITLE_SERVICE_URL", "http://127.0.0.1:8765"),
            mock.patch.object(videolink, "urlopen", side_effect=fake_urlopen),
        ):
            started, error = videolink.start_live_subtitle_session("session-1")

        self.assertTrue(started)
        self.assertIsNone(error)
        request, timeout = requests[0]
        self.assertEqual(timeout, 5)
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/sessions/session-1/start")


def fake_youtube_dl(info: dict) -> mock.MagicMock:
    """A yt_dlp.YoutubeDL replacement whose context manager yields `info`."""
    factory = mock.MagicMock()
    ydl = factory.return_value.__enter__.return_value
    ydl.extract_info.return_value = info
    ydl.sanitize_info.side_effect = lambda payload: payload
    return factory


class ResolverCoreTests(unittest.TestCase):
    """Pure resolver-core behavior that should survive the daemon migration."""

    def test_scored_candidates_prioritize_platform_and_jsonld_over_previews(self) -> None:
        page = """
        <meta property="og:video" content="https://cdn.example.com/preview-lowres.mp4">
        <iframe src="https://embed.example.com/player/real"></iframe>
        <script type="application/ld+json">
        {
          "@type": "VideoObject",
          "name": "Structured clip",
          "embedUrl": "https://json.example.com/watch/abc",
          "contentUrl": "https://cdn.example.com/live/master.m3u8"
        }
        </script>
        """

        def fake_match(url: str) -> str | None:
            return "Example" if "example.com/player" in url or "json.example.com" in url else None

        candidates = resolver_core.discover_scored_candidates(
            page,
            "https://news.example.com/story",
            match_extractor=fake_match,
        )

        self.assertEqual(
            [(candidate.kind, candidate.url, candidate.needs_probe) for candidate in candidates],
            [
                ("platform_embed", "https://embed.example.com/player/real", True),
                ("jsonld_platform", "https://json.example.com/watch/abc", True),
                ("direct_file", "https://cdn.example.com/live/master.m3u8", False),
                ("og_video", "https://cdn.example.com/preview-lowres.mp4", False),
            ],
        )
        self.assertEqual(
            resolver_core.discover_embed_candidate_urls(
                page,
                "https://news.example.com/story",
                match_extractor=fake_match,
            ),
            [
                "https://embed.example.com/player/real",
                "https://json.example.com/watch/abc",
            ],
        )

    def test_cache_key_includes_ytdlp_version_and_normalized_url(self) -> None:
        self.assertEqual(
            resolver_core.cache_key("example.com/watch?v=1", ytdlp_version="2026.test"),
            "2026.test|https://example.com/watch?v=1",
        )
        self.assertNotEqual(
            resolver_core.cache_key("https://example.com/watch?v=1", ytdlp_version="old"),
            resolver_core.cache_key("https://example.com/watch?v=1", ytdlp_version="new"),
        )

    def test_extraction_error_classification_is_actionable(self) -> None:
        cases = {
            "HTTP Error 403: forbidden; try cookies": "login_wall",
            "This video is not available in your country": "geo_blocked",
            "subscriber-only premium video": "paywall",
            "operation timed out": "timeout",
            "Unsupported URL: https://example.com/story": "no_video",
            "unexpected extractor failure": "unknown",
            None: "unknown",
        }
        for message, expected in cases.items():
            self.assertEqual(resolver_core.classify_extraction_error(message), expected)


class StreamResolutionTests(unittest.TestCase):
    """The live-subtitle flow must hand IINA and ffmpeg real stream URLs,
    selected by format protocol — URL appearance fails for platforms like
    YouTube whose stream URLs carry no filename extension."""

    YOUTUBE_LIKE_FORMATS = [
        {"url": "https://gv.example/prog-360", "protocol": "https",
         "vcodec": "avc1", "acodec": "mp4a", "height": 360},
        {"url": "https://gv.example/audio-low", "protocol": "https",
         "vcodec": "none", "acodec": "mp4a"},
        {"url": "https://gv.example/audio-best", "protocol": "https",
         "vcodec": "none", "acodec": "opus"},
        {"url": "https://gv.example/dash-frags", "protocol": "http_dash_segments",
         "vcodec": "vp9", "acodec": "none"},
        {"url": "https://gv.example/video-1080", "protocol": "https",
         "vcodec": "avc1", "acodec": "none", "height": 1080},
        {"url": "https://gv.example/video-2160", "protocol": "https",
         "vcodec": "vp9", "acodec": "none", "height": 2160},
    ]

    def setUp(self) -> None:
        if videolink is None:
            self.skipTest(f"videolink dependencies unavailable: {VIDEOLINK_IMPORT_ERROR}")
        videolink._RESOLVED_INFO_CACHE.clear()

    def tearDown(self) -> None:
        if videolink is not None:
            videolink._RESOLVED_INFO_CACHE.clear()

    def test_selects_capped_high_quality_display_and_best_audio_only_stream(self) -> None:
        info = {"formats": self.YOUTUBE_LIKE_FORMATS}
        with mock.patch.object(videolink.yt_dlp, "YoutubeDL", fake_youtube_dl(info)):
            streams = videolink.resolve_stream_urls(
                "https://www.youtube.com/watch?v=abc12345678", None)

        self.assertEqual(streams.display_url, "https://gv.example/video-1080")
        self.assertEqual(streams.audio_url, "https://gv.example/audio-best")
        self.assertEqual(streams.iina_audio_url, "https://gv.example/audio-best")

    def test_muxed_display_does_not_attach_duplicate_iina_audio(self) -> None:
        info = {
            "formats": [
                {"url": "https://gv.example/prog-720", "protocol": "https",
                 "vcodec": "avc1", "acodec": "mp4a", "height": 720},
                {"url": "https://gv.example/audio-best", "protocol": "https",
                 "vcodec": "none", "acodec": "opus"},
            ]
        }
        with mock.patch.object(videolink.yt_dlp, "YoutubeDL", fake_youtube_dl(info)):
            streams = videolink.resolve_stream_urls(
                "https://www.youtube.com/watch?v=abc12345678", None)

        self.assertEqual(streams.display_url, "https://gv.example/prog-720")
        self.assertEqual(streams.audio_url, "https://gv.example/audio-best")
        self.assertIsNone(streams.iina_audio_url)

    def test_generic_file_without_codec_info_streams_as_is(self) -> None:
        info = {"formats": [{"url": "https://cdn.example.com/stream", "protocol": "https"}]}
        with mock.patch.object(videolink.yt_dlp, "YoutubeDL", fake_youtube_dl(info)):
            streams = videolink.resolve_stream_urls("https://news.example.com/story", None)
        self.assertEqual(streams.display_url, "https://cdn.example.com/stream")
        self.assertEqual(streams.audio_url, "https://cdn.example.com/stream")
        self.assertIsNone(streams.iina_audio_url)

    def test_playlist_info_returns_none(self) -> None:
        info = {"entries": [{"id": "a"}, {"id": "b"}]}
        with mock.patch.object(videolink.yt_dlp, "YoutubeDL", fake_youtube_dl(info)):
            self.assertIsNone(
                videolink.resolve_stream_urls("https://example.com/playlist", None))

    def test_direct_media_path_url_skips_resolution_entirely(self) -> None:
        url = "https://cdn.example.com/live/master.m3u8?token=1"
        with mock.patch.object(
            videolink.yt_dlp, "YoutubeDL",
            side_effect=AssertionError("YoutubeDL must not be constructed"),
        ):
            streams = videolink.resolve_stream_urls(url, None)
        self.assertEqual(streams.display_url, url)
        self.assertEqual(streams.audio_url, url)
        self.assertIsNone(streams.iina_audio_url)

    def test_extraction_probe_results_are_reused_without_second_resolution(self) -> None:
        """ydl_extract caches full infos; the live flow must not resolve twice."""
        page_url = "https://www.youtube.com/watch?v=abc12345678"
        info = {"webpage_url": page_url, "title": "Clip",
                "extractor_key": "Youtube", "formats": self.YOUTUBE_LIKE_FORMATS}

        with mock.patch.object(videolink.yt_dlp, "YoutubeDL", fake_youtube_dl(info)):
            videos = videolink.ydl_extract("https://embed.example.com/player", None)
        self.assertEqual(videos, [("Clip", page_url)])

        with mock.patch.object(
            videolink.yt_dlp, "YoutubeDL",
            side_effect=AssertionError("second resolution must hit the cache"),
        ):
            streams = videolink.resolve_stream_urls(page_url, None)

        self.assertEqual(streams.display_url, "https://gv.example/video-1080")
        self.assertEqual(streams.audio_url, "https://gv.example/audio-best")
        self.assertEqual(streams.iina_audio_url, "https://gv.example/audio-best")


class ResolverRedesignTests(unittest.TestCase):
    """Regressions for the fast path, deadline budget, ordering, and dialogs."""

    def setUp(self) -> None:
        if videolink is None:
            self.skipTest(f"videolink dependencies unavailable: {VIDEOLINK_IMPORT_ERROR}")

    def test_fast_path_platform_url_skips_fetch_and_extraction(self) -> None:
        """A YouTube-like URL returns immediately: no HTML fetch, no yt-dlp run."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        with (
            mock.patch.object(videolink, "fetch_html",
                              side_effect=AssertionError("fetch_html must not be called")),
            mock.patch.object(videolink, "ydl_extract",
                              side_effect=AssertionError("ydl_extract must not be called")),
        ):
            videos, source = videolink.extract_videos_traced(url, None)

        self.assertEqual(source, "fast-path")
        self.assertEqual(videos, [(url, url)])

    def test_fast_path_direct_media_url(self) -> None:
        url = "https://vcdn02.spiegel.de/videos/Wa1zjie2-b8Tmknhy.mp4"
        with (
            mock.patch.object(videolink, "fetch_html",
                              side_effect=AssertionError("fetch_html must not be called")),
            mock.patch.object(videolink, "ydl_extract",
                              side_effect=AssertionError("ydl_extract must not be called")),
        ):
            videos, source = videolink.extract_videos_traced(url, None)

        self.assertEqual(source, "fast-path")
        self.assertEqual(videos, [(url, url)])

    def test_fast_path_ignores_media_filenames_in_query_strings(self) -> None:
        """?video=clip.mp4 in an article URL must not be mistaken for the video."""
        for url in (
            "https://news.example.com/article?video=clip.mp4",
            "https://example.com/watch?file=video.mp4&autoplay=1",
            "https://news.example.com/article?delivery=hls",
            "https://example.com/story#promo.m3u8",
        ):
            self.assertIsNone(videolink.fast_path_videos(url), url)

        # Real media paths (including BILD delivery manifests) still fast-path.
        for url in (
            "https://vcdn02.spiegel.de/videos/clip.mp4",
            "https://cdn.example.com/live/master.m3u8?token=abc",
            "https://hds.ak.token.bild.de/video/master.m3u8,delivery=hls",
        ):
            self.assertEqual(videolink.fast_path_videos(url), [(url, url)], url)

    def test_fast_path_denylists_bild_so_delivery_preference_runs(self) -> None:
        """BILD article URLs must NOT fast-path: the fragile-host swap to
        durable hds.ak.token.bild.de delivery manifests has to run."""
        url = "https://www.bild.de/video/clip/news/test-video-12345678.bild.html"

        name = videolink.specific_extractor_name(url)
        self.assertIsNotNone(name, "yt-dlp registry no longer claims BILD URLs?")
        self.assertIn("bild", name.lower())
        self.assertIsNone(videolink.fast_path_videos(url))

        page = '"https:\\/\\/hds.ak.token.bild.de\\/video\\/master.m3u8,delivery=hls"'

        def fake_ydl(target, cookies, deadline=None):
            if target == url:  # generic last resort resolves a fragile CDN URL
                return [("Clip", "https://bild.cdn.content-discovery.video/hls/clip.m3u8")]
            return []

        with (
            mock.patch.object(videolink, "fetch_html", return_value=page),
            mock.patch.object(videolink, "ydl_extract", side_effect=fake_ydl),
        ):
            videos, source = videolink.extract_videos_traced(url, None)

        self.assertEqual(source, "fallback")
        self.assertEqual(videos, [
            ("BILD delivery HLS",
             "https://hds.ak.token.bild.de/video/master.m3u8,delivery=hls"),
        ])

    def test_fast_path_excludes_collection_extractors(self) -> None:
        """Set/album/showcase pages keep the multi-video picker flow."""
        for url in (
            "https://soundcloud.com/some-artist/sets/some-set",
            "https://someartist.bandcamp.com/album/some-album",
            "https://vimeo.com/album/1234567",
        ):
            name = videolink.specific_extractor_name(url)
            self.assertIsNotNone(name, f"registry no longer claims {url}")
            self.assertIsNone(
                videolink.fast_path_videos(url),
                f"{url} (extractor {name!r}) must not fast-path",
            )

    def test_guardian_docsdata_row_matches_share_links_with_trackers(self) -> None:
        """?CMP=... share links and #fragments must still match the fullLink row."""
        row = {"fullLink": "https://www.theguardian.com/x/ng-interactive/doc/", "youTubeId": ""}
        for url in (
            "https://www.theguardian.com/x/ng-interactive/doc?CMP=share_btn_link",
            "https://www.theguardian.com/x/ng-interactive/doc/#top",
            "https://www.theguardian.com/x/ng-interactive/doc",
        ):
            self.assertTrue(
                videolink.guardian_docsdata_row_matches(
                    row, videolink.guardian_link_key(url), "other-doc"),
                url,
            )

    def test_deadline_timeouts_are_fractional_and_floor_out(self) -> None:
        # Nearly-expired budget: ydl_extract must not even construct a YoutubeDL.
        with mock.patch.object(
            videolink.yt_dlp, "YoutubeDL",
            side_effect=AssertionError("YoutubeDL must not be constructed"),
        ):
            self.assertEqual(
                videolink.ydl_extract("https://example.com/x", None, videolink.Deadline(0.2)),
                [],
            )

        # Fractional remaining time is passed through to curl, not rounded up.
        commands: list[list[str]] = []

        def fake_run(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="<html></html>", stderr="")

        with (
            mock.patch.object(videolink.shutil, "which", return_value="/usr/bin/curl"),
            mock.patch.object(videolink.subprocess, "run", side_effect=fake_run),
        ):
            videolink.fetch_html_with_curl("https://example.com/x", videolink.Deadline(2.5))

        command = commands[0]
        max_time = float(command[command.index("--max-time") + 1])
        self.assertLessEqual(max_time, 2.5)
        self.assertGreater(max_time, 1.0, "must not be floored/rounded to whole seconds")

    def test_iframe_candidate_outranks_og_video_preview(self) -> None:
        """A real embedded player must win over an og:video preview file."""
        page = """
        <meta property="og:video" content="https://cdn.example.com/preview-lowres.mp4">
        <iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ"></iframe>
        """
        embed_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

        def fake_ydl(url, cookies, deadline=None):
            if "youtube" in url:
                return [("Real embed", embed_url)]
            return []

        with (
            mock.patch.object(videolink, "fetch_html", return_value=page),
            mock.patch.object(videolink, "ydl_extract", side_effect=fake_ydl),
        ):
            videos, source = videolink.extract_videos_traced(
                "https://news.example.com/story", None)

        self.assertEqual(source, "fallback")
        self.assertEqual(videos[0], ("Real embed", embed_url))
        self.assertNotIn("preview-lowres.mp4", videos[0][1])

    def test_guardian_interactive_documentary_resolves_to_youtube(self) -> None:
        url = (
            "https://www.theguardian.com/global/ng-interactive/2026/jun/09/"
            "how-do-you-give-britains-hidden-army-a-break-is-mum-ok-documentary"
        )
        page = """
        <div id="docu-root" data-docu-id="is-mum-ok?"></div>
        <script>
        el.src = "https://interactive.guim.co.uk/atoms/2026/05/documentary-interactive/documentary-too-young-to-care/v/1780499670/app.js";
        </script>
        """
        app_js = 'var s={sheetId:"1jYb4okCk7I42wCoduH-o48DE-EKGajM8w-MTxQYxmck"};'
        docsdata = json.dumps({
            "sheets": {
                "documentaries": [
                    {
                        "docName": "is-mum-ok?",
                        "title": "Is Mum OK?",
                        "youTubeId": "ljMQCsaPukE",
                        "fullLink": url,
                    }
                ]
            }
        })

        def fake_fetch_text(target, deadline=None):
            if target == url:
                return page
            if target.endswith("/app.js"):
                return app_js
            if target.endswith("/1jYb4okCk7I42wCoduH-o48DE-EKGajM8w-MTxQYxmck.json"):
                return docsdata
            return ""

        with (
            mock.patch.object(videolink, "fetch_text", side_effect=fake_fetch_text),
            mock.patch.object(videolink, "ydl_extract",
                              side_effect=AssertionError("generic extraction must not be needed")),
        ):
            videos, source = videolink.extract_videos_traced(url, None)

        self.assertEqual(source, "fallback")
        self.assertEqual(videos, [("Is Mum OK?", "https://www.youtube.com/watch?v=ljMQCsaPukE")])

    def test_concurrent_probe_race_returns_before_slow_candidate(self) -> None:
        def fake_ydl(url, cookies, deadline=None):
            if url.startswith("https://slow"):
                time.sleep(3)
                return []
            return [("Fast", url)]

        start = time.monotonic()
        with mock.patch.object(videolink, "ydl_extract", side_effect=fake_ydl):
            videos = videolink.probe_candidates_concurrently(
                ["https://slow.example.com/embed", "https://fast.example.com/embed"],
                None,
                videolink.Deadline(10),
            )
        elapsed = time.monotonic() - start

        self.assertEqual(videos, [("Fast", "https://fast.example.com/embed")])
        self.assertLess(elapsed, 2.0, "probe race must not wait for the slow candidate")

    def test_deadline_expired_curl_makes_no_subprocess_call(self) -> None:
        calls: list = []
        with (
            mock.patch.object(videolink.shutil, "which", return_value="/usr/bin/curl"),
            mock.patch.object(videolink.subprocess, "run",
                              side_effect=lambda *a, **k: calls.append(a)),
        ):
            page = videolink.fetch_html_with_curl("https://example.com", videolink.Deadline(0))

        self.assertEqual(page, "")
        self.assertEqual(calls, [])

    def test_subtitle_mode_applescript_uses_real_newlines(self) -> None:
        scripts: list[str] = []

        def fake_run(command, **kwargs):
            scripts.append(command[2])
            return subprocess.CompletedProcess(command, 0, stdout="Live subtitles\n", stderr="")

        args = videolink.parse_args(["--subtitle-prompt"])
        with (
            mock.patch.object(videolink.shutil, "which", return_value="/usr/bin/osascript"),
            mock.patch.object(videolink.subprocess, "run", side_effect=fake_run),
        ):
            mode = videolink.resolve_subtitle_mode(args, "Title")

        self.assertEqual(mode, "live")
        script = scripts[0]
        self.assertNotIn("\\n", script, "literal backslash-n is an AppleScript syntax error")
        self.assertIn('\nif chosen is false then return "Cancel"\n', script)

    def test_open_without_subtitles_dialog_uses_real_newlines(self) -> None:
        scripts: list[str] = []

        def fake_run(command, **kwargs):
            scripts.append(command[2])
            return subprocess.CompletedProcess(command, 0, stdout="Open\n", stderr="")

        with (
            mock.patch.object(videolink.shutil, "which", return_value="/usr/bin/osascript"),
            mock.patch.object(videolink.subprocess, "run", side_effect=fake_run),
        ):
            answer = videolink.ask_open_without_subtitles("service down")

        self.assertTrue(answer)
        script = scripts[0]
        # Statements separated by REAL newlines...
        self.assertIn('with title "Video Subtitles"\nreturn button returned of result', script)
        # ...while the newline inside the prompt string uses AppleScript's \n escape.
        self.assertIn("\\nOpen video without subtitles?", script)


class LiveFlowTests(unittest.TestCase):
    """Ordering, budgets, and truthfulness of the live-subtitle hotkey flow."""

    def setUp(self) -> None:
        if videolink is None:
            self.skipTest(f"videolink dependencies unavailable: {VIDEOLINK_IMPORT_ERROR}")

    def test_previous_window_closed_only_after_successful_ipc_launch(self) -> None:
        state_dir = tempfile.mkdtemp(prefix="videolink-test-state-")
        state_file = os.path.join(state_dir, "last_iina_socket.txt")
        with open(state_file, "w", encoding="utf-8") as handle:
            handle.write("/tmp/vl-old.sock")

        calls: list[list[str]] = []

        def fake_which(name: str) -> str | None:
            return "/usr/local/bin/iina" if name == "iina" else None

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[0] == "pgrep":
                return subprocess.CompletedProcess(command, 0, stdout="4242\n", stderr="")
            if command[0] == "ps":
                return subprocess.CompletedProcess(
                    command, 0,
                    stdout="/Applications/IINA.app/Contents/MacOS/IINA --mpv-input-ipc-server=/tmp/vl-old.sock\n",
                    stderr="")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with (
            mock.patch.object(videolink.shutil, "which", side_effect=fake_which),
            mock.patch.object(videolink.subprocess, "run", side_effect=fake_run),
            mock.patch.object(videolink, "LAST_SUBTITLE_WINDOW_FILE", state_file),
        ):
            opened = videolink.open_in_iina(
                "https://cdn.example.com/v.m3u8", ipc_path="/tmp/vl-new.sock")

        self.assertTrue(opened)
        self.assertEqual(calls[0][0], "/usr/local/bin/iina",
                         "the NEW window must launch before the old one is touched")
        self.assertIn(["kill", "4242"], calls)
        with open(state_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "/tmp/vl-new.sock")

    def test_iina_launch_attaches_external_audio_for_video_only_display(self) -> None:
        calls: list[list[str]] = []

        def fake_which(name: str) -> str | None:
            return "/usr/local/bin/iina" if name == "iina" else None

        def fake_run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with (
            mock.patch.object(videolink.shutil, "which", side_effect=fake_which),
            mock.patch.object(videolink.subprocess, "run", side_effect=fake_run),
            isolated_state_file(),
        ):
            opened = videolink.open_in_iina(
                "https://gv.example/video-1080",
                ipc_path="/tmp/vl-new.sock",
                audio_url="https://gv.example/audio-best",
            )

        self.assertTrue(opened)
        self.assertIn("--audio-file=https://gv.example/audio-best", calls[0])

    def test_stream_resolution_overlaps_with_dialog_time(self) -> None:
        def slow_resolve(url, cookies, deadline=None):
            time.sleep(0.3)
            return videolink.StreamSelection("https://d.example/v", "https://a.example/a")

        with mock.patch.object(videolink, "resolve_stream_urls", side_effect=slow_resolve):
            wait = videolink.begin_stream_resolution("https://x.example/page", None)
            time.sleep(0.35)  # the human reading the dialog
            start = time.monotonic()
            streams = wait()
            waited = time.monotonic() - start

        self.assertEqual(streams.display_url, "https://d.example/v")
        self.assertEqual(streams.audio_url, "https://a.example/a")
        self.assertLess(waited, 0.1, "resolution must already be done after dialog time")

    def test_live_auto_launches_iina_before_slow_stream_resolution(self) -> None:
        """Main KPI: video playback must not wait for subtitle stream resolution."""
        events: list[tuple] = []

        def wait_for_streams(timeout_seconds=None):
            events.append(("wait_streams", timeout_seconds))
            if timeout_seconds is not None:
                return None
            return videolink.StreamSelection("https://d.example/v", "https://a.example/a")

        def fake_open(url, ipc_path=None, audio_url=None):
            events.append(("open", url, ipc_path, audio_url))
            return True

        def fake_create(media_url, title, language, model, ipc_path=None):
            events.append(("create_session", media_url, ipc_path))
            return {"id": "s1", "ipc_socket_path": ipc_path}, None

        def fake_start(session_id):
            events.append(("start_session", session_id))
            return True, None

        with (
            mock.patch.object(videolink.sys, "argv",
                              ["videolink.py", "--subtitle-auto", "https://news.example.com/story"]),
            mock.patch.object(videolink, "extract_with_timeout",
                              return_value=([("Clip", "https://x.example/watch")], "fast-path", None)),
            mock.patch.object(videolink, "copy_to_clipboard", return_value=True),
            mock.patch.object(videolink, "begin_stream_resolution", return_value=wait_for_streams),
            mock.patch.object(videolink, "make_iina_ipc_path", return_value="/tmp/vl-fast.sock"),
            mock.patch.object(videolink, "open_in_iina", side_effect=fake_open),
            mock.patch.object(videolink, "create_live_subtitle_session", side_effect=fake_create),
            mock.patch.object(videolink, "start_live_subtitle_session", side_effect=fake_start),
            mock.patch.object(videolink, "notify"),
        ):
            videolink.main()

        self.assertEqual(events[0], ("wait_streams", videolink.PLAYBACK_STREAM_GRACE_SECONDS))
        self.assertEqual(events[1], ("open", "https://x.example/watch", "/tmp/vl-fast.sock", None))
        self.assertEqual(events[2], ("wait_streams", None))
        self.assertEqual(events[3], ("create_session", "https://a.example/a", "/tmp/vl-fast.sock"))
        self.assertEqual(events[4], ("start_session", "s1"))

    def test_subtitles_flag_is_validated(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            videolink.parse_args(["--subtitles", "compile"])
        self.assertEqual(caught.exception.code, 2)

        self.assertEqual(videolink.parse_args(["--subtitles", "LIVE"]).subtitles, "live")
        self.assertEqual(videolink.parse_args(["--subtitles=none"]).subtitles, "none")

    def test_subtitle_mode_may_be_live_matches_resolution_outcomes(self) -> None:
        cases = [
            (["--subtitle-auto"], True),
            (["--subtitle-prompt"], True),
            (["--subtitles", "live"], True),
            (["--subtitles", "none"], False),
            (["--subtitle-prompt", "--no-subtitle-prompt"], False),
            ([], False),
        ]
        for argv, expected in cases:
            self.assertIs(
                videolink.subtitle_mode_may_be_live(videolink.parse_args(argv)),
                expected, argv)

    def test_compile_option_is_gone_from_subtitle_dialog(self) -> None:
        scripts: list[str] = []

        def fake_run(command, **kwargs):
            scripts.append(command[2])
            return subprocess.CompletedProcess(command, 0, stdout="Live subtitles\n", stderr="")

        args = videolink.parse_args(["--subtitle-prompt"])
        with (
            mock.patch.object(videolink.shutil, "which", return_value="/usr/bin/osascript"),
            mock.patch.object(videolink.subprocess, "run", side_effect=fake_run),
        ):
            videolink.resolve_subtitle_mode(args, "Title")

        self.assertNotIn("Compile", scripts[0], "the compile option was a no-op and must stay gone")

    def test_live_mode_with_broken_session_response_asks_instead_of_crashing(self) -> None:
        """A session dict missing ipc_socket_path used to raise KeyError.

        Video is now opened before subtitle session setup, so the failure path
        reports subtitle failure instead of asking whether to open fallback
        playback.
        """
        opened: list[str] = []
        notifications: list[str] = []

        with (
            mock.patch.object(videolink.sys, "argv",
                              ["videolink.py", "--subtitle-auto", "https://news.example.com/story"]),
            mock.patch.object(videolink, "extract_with_timeout",
                              return_value=([("Clip", "https://x.example/clip")], "primary", None)),
            mock.patch.object(videolink, "copy_to_clipboard", return_value=True),
            mock.patch.object(videolink, "begin_stream_resolution",
                              return_value=lambda timeout_seconds=None: videolink.StreamSelection(
                                  "https://d.example/v", "https://a.example/a")),
            mock.patch.object(videolink, "create_live_subtitle_session",
                              return_value=({"id": "s1"}, None)),  # no ipc_socket_path
            mock.patch.object(videolink, "make_iina_ipc_path", return_value="/tmp/vl-test.sock"),
            mock.patch.object(videolink, "open_in_iina",
                              side_effect=lambda url, **kwargs: opened.append(url) or True),
            mock.patch.object(videolink, "notify",
                              side_effect=lambda message, title="Video Link": notifications.append(message)),
        ):
            videolink.main()

        self.assertEqual(opened, ["https://d.example/v"])
        self.assertTrue(any("couldn't start live subtitles" in item for item in notifications))


class SubtitleServiceStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.service = importlib.import_module("subtitle_service")
        except (ModuleNotFoundError, SystemExit) as error:
            self.skipTest(f"subtitle service dependencies unavailable: {error}")

    def tearDown(self) -> None:
        if not hasattr(self, "service"):
            return
        with self.service.SESSION_LOCK:
            self.service.SESSIONS.clear()

    def make_session(self, state: str):
        service = self.service

        class FakeBackend:
            name = "mock"

            def close(self) -> None:
                pass

        return service.SubtitleSession(
            id=f"{state}-session",
            request=service.StartSubtitleRequest(media_url="https://cdn.example.com/video.m3u8"),
            backend=FakeBackend(),
            backend_info=service.BackendInfo(
                name="mock",
                device="test",
                model="small",
                acceleration_active=False,
                available=True,
            ),
            srt_path="/tmp/videolink-test.srt",
            state=state,
        )

    def test_completed_session_does_not_make_service_look_active(self) -> None:
        service = self.service
        with service.SESSION_LOCK:
            service.SESSIONS["completed-session"] = self.make_session("completed")

        with (
            mock.patch.object(
                service,
                "select_live_backend",
                return_value=(
                    None,
                    service.BackendInfo("mock", "test", "small", False, available=True),
                    [],
                ),
            ),
            mock.patch.object(service.WhisperXAlignmentBackend, "is_available", return_value=False),
        ):
            payload = service.service_status_payload()

        self.assertEqual(payload["state"], "idle")
        self.assertIsNone(payload["media_url"])
        self.assertEqual(payload["sessions"], ["completed-session"])

    def test_running_session_is_reported_as_current(self) -> None:
        service = self.service
        with service.SESSION_LOCK:
            service.SESSIONS["running-session"] = self.make_session("running")

        with mock.patch.object(service.WhisperXAlignmentBackend, "is_available", return_value=False):
            payload = service.service_status_payload()

        self.assertEqual(payload["state"], "running")
        self.assertEqual(payload["session_id"], "running-session")
        self.assertEqual(payload["media_url"], "https://cdn.example.com/video.m3u8")

    def test_chunk_seconds_default_uses_environment_backed_constant(self) -> None:
        service = self.service
        request = service.StartSubtitleRequest(media_url="https://cdn.example.com/video.m3u8")
        self.assertEqual(request.chunk_seconds, service.DEFAULT_CHUNK_SECONDS)


if __name__ == "__main__":
    unittest.main()
