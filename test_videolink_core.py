#!/usr/bin/env python3
"""Focused regression tests for the VideoLink hotkey core."""

from __future__ import annotations

import importlib
import json
import subprocess
import time
import unittest
from unittest import mock

try:
    import videolink
    VIDEOLINK_IMPORT_ERROR = None
except ModuleNotFoundError as error:
    videolink = None
    VIDEOLINK_IMPORT_ERROR = error


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
        self.assertEqual(timeout, 20)
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/sessions")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["media_url"], "https://cdn.example.com/video.m3u8")
        self.assertEqual(payload["iina_ipc_path"], "/tmp/vl-test.sock")
        self.assertEqual(payload["mode"], "live")

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


if __name__ == "__main__":
    unittest.main()
