#!/usr/bin/env python3
"""Focused regression tests for the VideoLink hotkey core."""

from __future__ import annotations

import importlib
import json
import subprocess
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
