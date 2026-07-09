#!/usr/bin/env python3
"""Grab a video link from a web page and copy it to the clipboard.

Keybind flow (macOS): copy a page URL, hit your hotkey. This reads the URL
from the clipboard, asks yt-dlp what videos are on the page, copies the link,
opens it in IINA (or shows a picker first if there are several), and posts a
notification.

Usage:
    videolink.py [url] [--cookies-from-browser BROWSER] [--html] [--subtitle-prompt|--subtitle-auto]

If no url is given it reads one from the clipboard.
--cookies-from-browser chrome|safari|firefox|edge|brave|chromium|opera|vivaldi
    unlocks auth-gated sites (reddit, instagram, private posts) by borrowing your
    browser's login cookies. (--cookies-from is kept as an alias.)
--html  copy an <a href> anchor tag instead of the plain URL (for rich-text
    targets like email or a CMS).
--subtitle-prompt  offer to open IINA with live subtitles through the local
    subtitle service if it is running.
--subtitle-auto  start live subtitles automatically (no confirmation dialog)
    when the local subtitle service is running and a backend is available.
--sweep-test [--seed N]  developer smoke test: run the extractor against a random
    sample of 50 news + 50 general public sites and print a summary. Copies
    nothing. --seed makes the site selection reproducible.
"""

from __future__ import annotations

import re
import os
import sys
import html
import json
import time
import uuid
import random
import shutil
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import yt_dlp

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Used only by the fallback (see extract_videos): some publishers wrap a YouTube
# or Vimeo video in their own player, so yt-dlp's generic extractor sees no embed.
# These find the underlying video id, which we then resolve through yt-dlp.
_YT_ID = r"[A-Za-z0-9_-]{11}"
EMBED_URL_PATTERNS = (
    re.compile(rf"youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=)({_YT_ID})", re.I),
    re.compile(rf"youtu\.be/({_YT_ID})", re.I),
)
VIMEO_EMBED_PATTERN = re.compile(r"player\.vimeo\.com/video/(\d+)", re.I)
# data-video-id is non-standard, so only trust it when the page clearly uses YouTube.
DATA_VIDEO_ID_PATTERN = re.compile(rf'data-video-id=["\']({_YT_ID})["\']', re.I)
DIRECT_VIDEO_EXTENSIONS = (
    ".mp4",
    ".webm",
    ".mov",
    ".m4v",
    ".avi",
    ".mkv",
    ".flv",
    ".wmv",
    ".m3u8",
    ".mpd",
)
DIRECT_VIDEO_PATTERN = re.compile(
    rf"({'|'.join(re.escape(extension) for extension in DIRECT_VIDEO_EXTENSIONS)})(?:$|[?#&=/])",
    re.I,
)
DIRECT_MEDIA_EXTRACTORS = {"Generic", "HTML5MediaEmbed"}
FRAGILE_RESOLVED_MEDIA_HOSTS = {"bild.cdn.content-discovery.video"}
PREFERRED_PAGE_DELIVERY_HOSTS = {"hds.ak.token.bild.de"}
DELIVERY_MARKER_PATTERN = re.compile(r"(?:,|[?&])delivery=(hls|mpd|pmd)\b", re.I)

# Second-stage fallback: narrow patterns for the big hosted players (Brightcove,
# JWPlayer, Kaltura). We only turn these into canonical URLs that yt-dlp's own
# extractors understand — yt-dlp still does all the actual resolution.
BRIGHTCOVE_PLAYER_URL = re.compile(
    r"https?://players\.brightcove\.net/\d+/[\w-]+/index\.html\?[^\"'<>\s]*"
    r"(?:video|playlist)Id=[\w:.-]+",
    re.I,
)
BRIGHTCOVE_TAG = re.compile(r"<(?:video-js|video|iframe)\b[^>]*\bdata-account=[^>]*>", re.I)
BRIGHTCOVE_ACCOUNT = re.compile(r"data-account=[\"']?(\d+)", re.I)
BRIGHTCOVE_PLAYER = re.compile(r"data-player=[\"']?([\w-]+)", re.I)
BRIGHTCOVE_VIDEO_ID = re.compile(r"data-video-id=[\"']?(\d+|ref:[\w-]+)", re.I)

JWPLAYER_URL_ID = re.compile(
    r"(?:cdn\.jwplayer\.com|content\.jwplatform\.com)/"
    r"(?:players|previews|manifests|thumbs|tracks|feeds|v2/media)/([A-Za-z0-9]{8})",
    re.I,
)
JWPLAYER_MEDIA_ID = re.compile(r'"mediaid"\s*:\s*"([A-Za-z0-9]{8})"', re.I)

KALTURA_PLAYER_URL = re.compile(
    r"https?://[^\"'<>\s]*kaltura\.com/[^\"'<>\s]*(?:entry_?id|/p/\d+/)[^\"'<>\s]*", re.I
)
KALTURA_PARTNER = re.compile(
    r"(?:partner_?id[\"']?\s*[:=]\s*[\"']?|/p/|/partner_id/|wid=_)(\d{3,9})", re.I
)
KALTURA_ENTRY = re.compile(
    r"(?:entry_?id[\"']?\s*[:=]\s*[\"']?|/entry_id/)([01]_[0-9a-zA-Z]{8})", re.I
)
GUARDIAN_INTERACTIVE_APP_URL = re.compile(
    r"(?:https?:)?//interactive\.guim\.co\.uk/atoms/[^\"'<>\s]+/app\.js", re.I
)
GUARDIAN_SHEET_ID = re.compile(r"sheetId\s*:\s*[\"']([A-Za-z0-9_-]+)[\"']")
GUARDIAN_DOCU_ID = re.compile(r"\bdata-docu-id\s*=\s*[\"']([^\"']+)[\"']", re.I)
# yt-dlp can read cookies from these browsers. We validate against this list so a
# typo fails loudly instead of silently extracting without authentication.
SUPPORTED_BROWSERS = (
    "chrome", "safari", "firefox", "edge", "brave", "chromium", "opera", "vivaldi",
)

# A web page that pulls in lots of requests can take a long time. A keybind
# tool must never hang, so we abandon extraction after this many seconds.
EXTRACT_TIMEOUT_SECONDS = 12

# The live-subtitle flow stacks three network stages (extract, stream resolve,
# session create). Each gets its own hard cap so the worst case stays ~30s
# instead of the ~44s the old per-stage defaults added up to.
LIVE_RESOLVE_TIMEOUT_SECONDS = 10
SESSION_CREATE_TIMEOUT_SECONDS = 8

# One HTML fetch may only consume a short slice of the overall budget, so the
# candidate probes and the generic last resort still have time to run.
FETCH_BUDGET_CAP_SECONDS = 6
# Embedded-player candidates probed at once; first usable result wins.
PROBE_CANDIDATE_LIMIT = 8
PROBE_WORKER_CAP = 4
# Extractors that claim playlist/channel/search/collection pages. Those still
# go through the normal flow so the multi-video picker keeps working.
FAST_PATH_EXCLUDED_MARKERS = (
    "playlist", "tab", "channel", "user", "search", "feed", "album", ":set",
)
# Extractors whose page flow must always run even for a single video, because
# their resolved media needs post-processing (e.g. BILD's fragile CDN URLs are
# swapped for durable hds.ak.token.bild.de delivery manifests).
FAST_PATH_DENYLISTED_EXTRACTORS = {"bild"}
# Below this remaining budget a stage is treated as expired instead of being
# rounded up to a whole-second timeout that could overrun the global budget.
DEADLINE_FLOOR_SECONDS = 0.25
SUBTITLE_SERVICE_URL = os.environ.get(
    "VIDEOLINK_SUBTITLE_SERVICE",
    "http://127.0.0.1:8765",
).rstrip("/")
IINA_BUNDLED_CLI = "/Applications/IINA.app/Contents/MacOS/iina-cli"

# VIDEOLINK_TIMINGS=1 prints per-stage wall-clock marks to stderr, measured
# from process start — the closest observable proxy for the hotkey press.
TIMINGS_ENABLED = os.environ.get("VIDEOLINK_TIMINGS") == "1"
_TIMING_START = time.monotonic()


def mark_timing(stage: str) -> None:
    if TIMINGS_ENABLED:
        print(f"[timing] {time.monotonic() - _TIMING_START:6.2f}s  {stage}",
              file=sys.stderr, flush=True)


class QuietLogger:
    """Swallow yt-dlp's own stderr chatter; we report via notifications instead."""

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "logger": QuietLogger(),
    "skip_download": True,
    "socket_timeout": 10,
    "retries": 0,
    "extractor_retries": 0,
    "noprogress": True,
    # Don't deep-resolve every item of a playlist/channel — just list them.
    "extract_flat": "in_playlist",
    "playlist_items": "1-25",
    "ignore_no_formats_error": True,
}


class HTMLMediaSourceParser(HTMLParser):
    """Collect direct media URLs from ordinary HTML media/meta tags."""

    def __init__(self, page_url: str):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.sources: list[tuple[str, str]] = []
        self.json_ld_blocks: list[str] = []
        self._in_json_ld = False
        self._json_ld_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value is not None}
        tag = tag.lower()

        if tag == "script" and "ld+json" in (values.get("type") or "").lower():
            self._in_json_ld = True
            self._json_ld_parts = []
            return

        if tag in {"source", "video"}:
            self.add_source(
                values.get("src"),
                values.get("title") or values.get("label") or values.get("res") or values.get("type"),
            )
            return

        if tag == "meta":
            meta_name = (values.get("property") or values.get("name") or "").lower()
            if meta_name in {
                "og:video",
                "og:video:url",
                "og:video:secure_url",
                "twitter:player:stream",
            }:
                self.add_source(values.get("content"), meta_name)

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._json_ld_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_json_ld:
            block = "".join(self._json_ld_parts).strip()
            if block:
                self.json_ld_blocks.append(block)
            self._in_json_ld = False
            self._json_ld_parts = []

    def add_source(self, raw_url: str | None, label: str | None) -> None:
        if not raw_url:
            return
        try:
            media_url = urljoin(self.page_url, html.unescape(raw_url.strip()))
        except ValueError:
            return
        if looks_like_direct_media_url(media_url):
            title = f"Direct media {label}".strip() if label else "Direct media"
            self.sources.append((title, media_url))


# --- platform glue (clipboard + notifications, macOS first) ------------------

def read_clipboard() -> str:
    for command in (["pbpaste"], ["wl-paste"], ["xclip", "-selection", "clipboard", "-o"]):
        if shutil.which(command[0]):
            try:
                return subprocess.run(command, capture_output=True, text=True, check=True).stdout
            except (OSError, subprocess.CalledProcessError):
                continue
    return ""


def copy_to_clipboard(text: str) -> bool:
    for command in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"],
                    ["xsel", "--clipboard", "--input"], ["clip"]):
        if shutil.which(command[0]):
            try:
                subprocess.run(command, input=text, text=True, check=True)
                return True
            except (OSError, subprocess.CalledProcessError):
                continue
    return False


def iina_cli_commands(url: str, mpv_args: list[str]) -> list[list[str]]:
    commands = []
    window_args = ["--separate-windows"] if mpv_args else []

    for cli_name in ("iina", "iina-cli"):
        cli_path = shutil.which(cli_name)
        if cli_path:
            commands.append([cli_path, "--no-stdin", *window_args, *mpv_args, url])

    if sys.platform == "darwin" and os.path.exists(IINA_BUNDLED_CLI):
        commands.append([IINA_BUNDLED_CLI, "--no-stdin", *window_args, *mpv_args, url])

    return commands


LAST_SUBTITLE_WINDOW_FILE = os.path.join(
    os.path.expanduser("~/Library/Application Support/VideoLink"), "last_iina_socket.txt"
)


def close_previous_subtitle_window() -> None:
    # Subtitle sessions force --separate-windows (see iina_cli_commands), so
    # unlike plain playback (which reuses IINA's single window normally),
    # every subtitle-enabled keybind press spawns a brand-new IINA process
    # that never gets closed on its own. The use case is always one video at
    # a time, so close whichever one this tool opened last before opening a
    # new one - otherwise IINA processes (and their GPU/network/CPU use) pile
    # up silently across a session, exactly the kind of thing that makes
    # later attempts flaky.
    try:
        with open(LAST_SUBTITLE_WINDOW_FILE, "r", encoding="utf-8") as handle:
            fingerprint = handle.read().strip()
    except OSError:
        return
    if not fingerprint:
        return
    try:
        result = subprocess.run(["pgrep", "-f", fingerprint], capture_output=True, text=True, check=False)
    except OSError:
        return
    for pid_str in result.stdout.split():
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        check = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False)
        if "IINA" not in check.stdout:
            continue  # the pid was reused for something else since - don't touch it
        try:
            subprocess.run(["kill", str(pid)], check=False)
        except OSError:
            pass


def remember_subtitle_window(ipc_path: str) -> None:
    try:
        os.makedirs(os.path.dirname(LAST_SUBTITLE_WINDOW_FILE), exist_ok=True)
        with open(LAST_SUBTITLE_WINDOW_FILE, "w", encoding="utf-8") as handle:
            handle.write(ipc_path)
    except OSError:
        pass


def open_in_iina(
    url: str,
    ipc_path: str | None = None,
    audio_url: str | None = None,
) -> bool:
    mpv_args = []
    if ipc_path:
        mpv_args.append(f"--mpv-input-ipc-server={ipc_path}")
    if audio_url:
        mpv_args.append(f"--audio-file={audio_url}")
    commands = iina_cli_commands(url, mpv_args)

    # The macOS `open -a ... --args` path does not reliably pass mpv IPC options
    # to IINA, so use it only when subtitles are not requested.
    fallback_commands: list[list[str]] = []
    if not ipc_path and sys.platform == "darwin" and shutil.which("open"):
        fallback_commands.append(["open", "-a", "IINA", url])

    for command in [*commands, *fallback_commands]:
        try:
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if ipc_path:
                # Close the old window only once the new launch has succeeded:
                # closing first would trade a working video for a failed one
                # whenever the launch errors. Order matters — close reads the
                # OLD fingerprint before remember overwrites it.
                close_previous_subtitle_window()
                remember_subtitle_window(ipc_path)
            return True
        except (OSError, subprocess.CalledProcessError):
            continue

    return False


def notify(message: str, title: str = "Video Link") -> None:
    """Post a desktop notification on macOS; fall back to printing."""
    if shutil.which("osascript"):
        script = f'display notification {applescript_string(message)} with title {applescript_string(title)}'
        try:
            subprocess.run(["osascript", "-e", script], check=True)
            return
        except (OSError, subprocess.CalledProcessError):
            pass
    print(f"{title}: {message}")


def choose_from_list(labels: list[str], prompt: str) -> int | None:
    """Native macOS picker; falls back to a terminal prompt. Returns an index."""
    if shutil.which("osascript"):
        quoted = ", ".join(applescript_string(label) for label in labels)
        script = (
            f'set chosen to choose from list {{{quoted}}} '
            f'with prompt {applescript_string(prompt)} without multiple selections allowed\n'
            'if chosen is false then return "\\n"\n'
            'return item 1 of chosen'
        )
        try:
            result = subprocess.run(["osascript", "-e", script],
                                    capture_output=True, text=True, check=True)
            picked = result.stdout.strip("\n")
            if not picked:
                return None
            if picked in labels:
                return labels.index(picked)
        except (OSError, subprocess.CalledProcessError):
            pass

    return choose_in_terminal(labels, prompt)


def choose_in_terminal(labels: list[str], prompt: str) -> int | None:
    print(f"{prompt}\n")
    for index, label in enumerate(labels, 1):
        print(f"{index}. {label}")
    print()
    while True:
        choice = input("Choose a number (or q to quit): ").strip().lower()
        if choice in {"q", "quit", "exit", ""}:
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(labels):
            return int(choice) - 1
        print("Invalid choice. Try again.")


def applescript_string(value: str) -> str:
    """Quote a Python string as an AppleScript string literal.

    Real newlines in the value become AppleScript's "\\n" escape — a raw line
    break inside an AppleScript string literal is a syntax error.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def make_iina_ipc_path() -> str:
    # Not tempfile.gettempdir(): when this runs from a macOS Shortcuts "Run
    # Shell Script" action, $TMPDIR points into that helper's own sandboxed
    # container, which IINA (a separate, unrelated app) cannot bind a socket
    # into — mpv silently fails to create it there. /tmp is the classic
    # shared, non-sandboxed location every app can read and write.
    path = os.path.join("/tmp", f"vl-{uuid.uuid4().hex[:8]}.sock")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    return path




# --- core --------------------------------------------------------------------

class Deadline:
    """One monotonic budget shared by every stage of a resolver run.

    Every socket-level operation asks for `remaining(cap=...)` so no single
    stage (fetch, probe, generic extraction) can overrun the hotkey budget.
    """

    def __init__(self, seconds: float):
        self._end = time.monotonic() + seconds

    def remaining(self, cap: float | None = None) -> float:
        left = max(0.0, self._end - time.monotonic())
        return min(left, cap) if cap is not None else left

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0


# Built once per process; ~0.2s the first time, then effectively free.
_SPECIFIC_EXTRACTORS: list | None = None


def specific_extractor_name(url: str) -> str | None:
    """Name of the non-generic yt-dlp extractor that claims this URL.

    Purely offline: matches the URL against the installed extractor registry's
    compiled patterns. No network traffic.
    """
    global _SPECIFIC_EXTRACTORS
    if _SPECIFIC_EXTRACTORS is None:
        from yt_dlp.extractor import gen_extractor_classes
        _SPECIFIC_EXTRACTORS = [
            ie for ie in gen_extractor_classes()
            if ie.IE_NAME not in ("generic", "html5")
        ]
    for ie in _SPECIFIC_EXTRACTORS:
        try:
            if ie.suitable(url):
                return ie.IE_NAME
        except Exception:
            continue
    return None


def fast_path_videos(page_url: str) -> list[tuple[str, str]] | None:
    """Zero-network answer for URLs that already are the video link.

    Direct media URLs (.m3u8/.mpd/.mp4/...) and URLs claimed by a specific
    yt-dlp extractor are returned as-is: the durable original URL is what the
    hotkey copies and what IINA opens, so resolving formats would only add
    seconds of network work whose output gets discarded. Playlist-ish
    extractor matches are excluded so the multi-video picker still works.
    """
    if is_direct_media_page_url(page_url):
        return [(page_url, page_url)]

    name = specific_extractor_name(page_url)
    if name:
        lowered = name.lower()
        if lowered in FAST_PATH_DENYLISTED_EXTRACTORS:
            return None
        if not any(marker in lowered for marker in FAST_PATH_EXCLUDED_MARKERS):
            return [(page_url, page_url)]
    return None


def normalize_page_url(raw_url: str) -> str:
    page_url = raw_url.strip()
    if page_url and not urlparse(page_url).scheme:
        return f"https://{page_url.lstrip('/')}"
    return page_url


def looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(parsed.scheme in {"http", "https"} and parsed.netloc and "." in parsed.netloc)


def is_direct_media_page_url(value: str | None) -> bool:
    """Strict gate for the zero-network fast path: only the URL *path* counts.

    A media filename inside a query string (?video=clip.mp4) is routinely part
    of an ordinary article URL, so it must never short-circuit extraction —
    copying such a page would hand IINA an HTML document. The broader
    looks_like_direct_media_url stays in use where the input is already a
    candidate media URL (format lists, <video src>, JSON-LD contentUrl).
    """
    if not value:
        return False

    normalized = html.unescape(value)
    if any(character in normalized for character in "\"'<>"):
        return False

    try:
        parsed = urlparse(normalized)
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False

    return (
        parsed.path.lower().endswith(DIRECT_VIDEO_EXTENSIONS)
        # BILD delivery manifests carry ",delivery=hls" inside the path.
        or delivery_kind(normalized) is not None
    )


def looks_like_direct_media_url(value: str | None) -> bool:
    if not value:
        return False

    normalized = html.unescape(value).lower()

    if any(character in normalized for character in "\"'<>"):
        return False

    try:
        parsed = urlparse(normalized)
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False

    if parsed.path.endswith(DIRECT_VIDEO_EXTENSIONS):
        return True

    return (
        bool(DIRECT_VIDEO_PATTERN.search(normalized))
        or "delivery=hls" in normalized
        or "delivery=mpd" in normalized
    )


def extract_videos(page_url: str, cookies_browser: str | None) -> list[tuple[str, str]]:
    """Return (title, url) for each video on the page (see extract_videos_traced)."""
    return extract_videos_traced(page_url, cookies_browser)[0]


def extract_videos_traced(
    page_url: str, cookies_browser: str | None, deadline: Deadline | None = None
) -> tuple[list[tuple[str, str]], str]:
    """Return (videos, source): 'fast-path', 'primary', 'fallback', or 'none'.

    1. Fast path (zero network): direct media URLs and URLs claimed by a
       specific yt-dlp extractor are returned as-is.
    2. Otherwise fetch the page ONCE (deadline-capped) and probe real
       embedded-player candidates (iframe/object/embed + canonical YouTube/
       Vimeo ids + Brightcove/JWPlayer/Kaltura) concurrently through yt-dlp;
       the first usable result wins.
    3. Only after embedded players fail, fall back to direct HTML media,
       JSON-LD, and Open Graph sources — so an og:video preview can never
       outrank a real embed.
    4. Generic yt-dlp extraction runs last, bounded by the remaining budget.
    """
    deadline = deadline or Deadline(EXTRACT_TIMEOUT_SECONDS)

    fast = fast_path_videos(page_url)
    if fast:
        return fast, "fast-path"

    page = fetch_html(page_url, deadline)
    if page:
        embed_candidates = discover_embed_candidate_urls(page, page_url)
        videos = probe_candidates_concurrently(embed_candidates, cookies_browser, deadline)
        if videos:
            return videos, "fallback"

        direct_videos = discover_html_media_sources(page, page_url)
        if direct_videos:
            return direct_videos, "fallback"

        guardian_videos = discover_guardian_interactive_videos(page, page_url, deadline)
        if guardian_videos:
            return guardian_videos, "fallback"

    if not deadline.expired:
        videos = ydl_extract(page_url, cookies_browser, deadline)
        if videos:
            if has_fragile_resolved_media_url(videos) and page:
                delivery_videos = discover_page_delivery_videos(page, page_url)
                if delivery_videos:
                    return delivery_videos, "fallback"
            return videos, "primary"

    return [], "none"


EMBED_TAG_SRC = re.compile(
    r"<(?:iframe|embed|object)\b[^>]*?\s(?:src|data)\s*=\s*[\"']([^\"']+)[\"']", re.I
)


def discover_embed_candidate_urls(page: str, page_url: str) -> list[str]:
    """Embedded-player candidate URLs, best first.

    iframe/object/embed sources are retained only when a specific (non-generic)
    installed yt-dlp extractor recognizes them — matched offline, no network.
    Canonical YouTube/Vimeo ids and reconstructed hosted-player URLs
    (Brightcove/JWPlayer/Kaltura) are recognized by construction.
    """
    urls = list(discover_embedded_urls(page))

    for match in EMBED_TAG_SRC.finditer(page):
        try:
            candidate = urljoin(page_url, html.unescape(match.group(1)).strip())
        except ValueError:
            continue
        if candidate.startswith(("http://", "https://")) and specific_extractor_name(candidate):
            urls.append(candidate)

    urls.extend(discover_player_urls(page))
    return dedupe_preserve(urls)[:PROBE_CANDIDATE_LIMIT]


def probe_candidates_concurrently(
    candidate_urls: list[str],
    cookies_browser: str | None,
    deadline: Deadline,
    worker_cap: int = PROBE_WORKER_CAP,
) -> list[tuple[str, str]]:
    """Probe candidates through yt-dlp in parallel; first usable result wins.

    Daemon threads (matching extract_with_timeout's pattern) gated by a small
    semaphore. The waiter is released by the FIRST non-empty result — a slow
    candidate cannot delay a fast one — or when all probes finish empty, or
    when the deadline runs out. If several results raced in, the one from the
    highest-ranked candidate is returned.
    """
    if not candidate_urls or deadline.expired:
        return []

    order = {url: index for index, url in enumerate(candidate_urls)}
    results: dict[str, list[tuple[str, str]]] = {}
    pending = set(candidate_urls)
    lock = threading.Lock()
    settled = threading.Event()
    gate = threading.Semaphore(worker_cap)

    def probe(url: str) -> None:
        with gate:
            if settled.is_set() or deadline.expired:
                videos: list[tuple[str, str]] = []
            else:
                try:
                    videos = ydl_extract(url, cookies_browser, deadline)
                except Exception:
                    videos = []
        with lock:
            pending.discard(url)
            if videos:
                results[url] = videos
                settled.set()
            elif not pending:
                settled.set()

    for url in candidate_urls:
        threading.Thread(target=probe, args=(url,), daemon=True).start()

    settled.wait(deadline.remaining())

    with lock:
        if not results:
            return []
        best = min(results, key=order.__getitem__)
        return dedupe_videos(results[best])


def ydl_extract(
    target_url: str, cookies_browser: str | None, deadline: Deadline | None = None
) -> list[tuple[str, str]]:
    """Resolve a single URL through yt-dlp. Returns [] if it finds nothing."""
    opts = dict(YDL_OPTS)
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    if deadline is not None:
        remaining = deadline.remaining(cap=10.0)
        if remaining <= DEADLINE_FLOOR_SECONDS:
            return []
        # Fractional pass-through: rounding up to 1s could overrun the budget.
        opts["socket_timeout"] = remaining

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.sanitize_info(ydl.extract_info(target_url, download=False))
    except yt_dlp.utils.DownloadError:
        return []  # unsupported / no video on this URL — let the caller try a fallback

    entries = info.get("entries")
    if entries is None and info.get("formats"):
        cache_resolved_info(info, target_url)
    items = list(entries) if entries is not None else [info]

    videos: list[tuple[str, str]] = []
    for item in items:
        if not item:
            continue
        direct_formats = direct_media_formats(item)
        if direct_formats:
            title = item.get("title") or item.get("webpage_url") or target_url
            for fmt in direct_formats:
                videos.append((format_title(title, fmt), fmt["url"]))
            continue

        # webpage_url is durable and always present when we found something;
        # direct format URLs aren't always available and tend to expire.
        url = item.get("webpage_url") or item.get("url")
        if url:
            videos.append((item.get("title") or url, url))
    return dedupe_videos(videos)


# Fully-resolved single-video infos from this run, keyed by every URL that
# names them. Lets the live-subtitle flow reuse the extraction pass's yt-dlp
# work instead of paying for a second full resolution of the same video.
_RESOLVED_INFO_CACHE: dict[str, dict] = {}
_RESOLVED_INFO_CACHE_LIMIT = 64


def cache_resolved_info(info: dict, target_url: str) -> None:
    if len(_RESOLVED_INFO_CACHE) >= _RESOLVED_INFO_CACHE_LIMIT:
        _RESOLVED_INFO_CACHE.clear()  # tiny process-lifetime cache; keep it dumb
    for key in {target_url, info.get("webpage_url"), info.get("original_url")}:
        if key:
            _RESOLVED_INFO_CACHE[key] = info


# ffmpeg and IINA can stream these yt-dlp protocols straight from a URL.
# http_dash_segments is a fragment list, not a fetchable stream, so it's out.
NON_STREAMABLE_PROTOCOLS = {"http_dash_segments"}
DISPLAY_HEIGHT_CAP = 1080


class StreamSelection(NamedTuple):
    display_url: str
    audio_url: str
    iina_audio_url: str | None = None


def format_protocol(fmt: dict) -> str:
    protocol = str(fmt.get("protocol") or "").lower()
    if protocol:
        return protocol
    url = str(fmt.get("url") or "").lower()
    if ".m3u8" in url:
        return "m3u8"
    if url.startswith(("http://", "https://")):
        return "https"
    return ""


def is_streamable_format(fmt) -> bool:
    if not isinstance(fmt, dict) or not fmt.get("url"):
        return False
    protocol = format_protocol(fmt)
    if protocol in NON_STREAMABLE_PROTOCOLS:
        return False
    return protocol.startswith(("m3u8", "http"))


def format_has_video(fmt: dict) -> bool:
    return fmt.get("vcodec") != "none"  # None/absent means unknown, not absent


def format_has_audio(fmt: dict) -> bool:
    return fmt.get("acodec") != "none"


def format_height(fmt: dict) -> int | None:
    height = fmt.get("height")
    return height if isinstance(height, int) else None


def select_stream_urls(info: dict) -> StreamSelection | None:
    """Pick playback and transcription streams from a resolved yt-dlp info dict.

    yt-dlp sorts formats worst-to-best, so the LAST match wins. The two
    consumers want different things: IINA gets a high-quality video stream
    capped at 1080p, with external audio attached when the video stream is
    video-only; ffmpeg only feeds audio to whisper, so an audio-only stream
    avoids hauling video bytes that get thrown away. Judging by format
    *protocol* instead of URL appearance is what makes this work for platforms
    whose stream URLs carry no filename extension (googlevideo, etc.).
    """
    formats = [fmt for fmt in info.get("formats") or [] if is_streamable_format(fmt)]
    if not formats:
        top_level = {key: info.get(key) for key in ("url", "protocol", "vcodec", "acodec")}
        if not is_streamable_format(top_level):
            return None
        formats = [top_level]

    video_formats = [fmt for fmt in formats if format_has_video(fmt)]
    if not video_formats:
        return None
    capped_video_formats = [
        fmt for fmt in video_formats
        if (height := format_height(fmt)) is None or height <= DISPLAY_HEIGHT_CAP
    ]
    display = (capped_video_formats or video_formats)[-1]

    audio_only = [fmt for fmt in formats if fmt.get("vcodec") == "none" and format_has_audio(fmt)]
    audio = audio_only[-1] if audio_only else display
    iina_audio_url = (
        audio["url"]
        if not format_has_audio(display) and audio is not display and format_has_audio(audio)
        else None
    )
    return StreamSelection(display["url"], audio["url"], iina_audio_url)


def resolve_stream_urls(
    url: str, cookies_browser: str | None, deadline: Deadline | None = None
) -> StreamSelection | None:
    """Resolve a chosen video to (display_url, audio_url) real stream URLs.

    For most sites, ydl_extract() intentionally returns the durable webpage_url
    instead of a format URL so a copied/pasted link doesn't expire. IINA can
    usually still open that page URL itself (it has its own bundled resolver),
    but ffmpeg (used by the subtitle pipeline) cannot, and IINA's own resolver
    is sometimes slower or less capable than ours — e.g. it can leave the mpv
    IPC socket unbound for longer than we wait for it. So when live subtitles
    are requested, we resolve here and hand IINA and the subtitle service real
    stream URLs instead.
    """
    if is_direct_media_page_url(url):
        return StreamSelection(url, url)

    info = _RESOLVED_INFO_CACHE.get(url)
    if info is None:
        opts = dict(YDL_OPTS)
        if cookies_browser:
            opts["cookiesfrombrowser"] = (cookies_browser,)
        if deadline is not None:
            remaining = deadline.remaining(cap=10.0)
            if remaining <= DEADLINE_FLOOR_SECONDS:
                return None
            opts["socket_timeout"] = remaining
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.sanitize_info(ydl.extract_info(url, download=False))
        except yt_dlp.utils.DownloadError:
            info = None

    if not info or info.get("entries"):
        # Unresolvable or a playlist page. If the chosen URL itself already
        # looks like media (og:video, JSON-LD, format URL), stream it as-is.
        return StreamSelection(url, url) if looks_like_direct_media_url(url) else None

    streams = select_stream_urls(info)
    if streams:
        return streams
    return StreamSelection(url, url) if looks_like_direct_media_url(url) else None


def begin_stream_resolution(url: str, cookies_browser: str | None):
    """Start resolving stream URLs in the background; returns a wait() callable.

    Kicked off BEFORE the subtitle dialog, so the network resolution runs
    while the human is choosing — by the time "Live subtitles" is picked the
    streams are usually already there. wait() carries the same never-hang
    guarantee as extract_with_timeout: it returns None once the resolution
    budget (plus a small grace period for the deadline-capped sockets to
    unwind) is exhausted.
    """
    result: dict[str, object] = {}
    deadline = Deadline(LIVE_RESOLVE_TIMEOUT_SECONDS)

    def worker():
        try:
            result["streams"] = resolve_stream_urls(url, cookies_browser, deadline)
        except Exception:
            result["streams"] = None

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    def wait() -> StreamSelection | None:
        thread.join(deadline.remaining() + 2.0)
        return result.get("streams") if not thread.is_alive() else None

    return wait


def direct_media_formats(item: dict) -> list[dict]:
    """Direct media URLs from wrapper-page extractors, not canonical platforms."""
    if item.get("extractor_key") not in DIRECT_MEDIA_EXTRACTORS:
        return []

    formats = [
        fmt
        for fmt in item.get("formats") or []
        if isinstance(fmt, dict) and looks_like_direct_media_url(fmt.get("url"))
    ]

    if formats:
        return formats

    url = item.get("url")
    if looks_like_direct_media_url(url):
        return [{"url": url, "format_id": item.get("format_id"), "ext": item.get("ext")}]

    return []


def format_title(title: str, fmt: dict) -> str:
    details = []

    if fmt.get("format_id"):
        details.append(str(fmt["format_id"]))
    if fmt.get("height"):
        details.append(f'{fmt["height"]}p')
    if fmt.get("ext"):
        details.append(str(fmt["ext"]))

    return f"{title} ({', '.join(details)})" if details else title


def has_fragile_resolved_media_url(videos: list[tuple[str, str]]) -> bool:
    return any(is_fragile_resolved_media_url(url) for _title, url in videos)


def is_fragile_resolved_media_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    return (parsed.hostname or "").lower() in FRAGILE_RESOLVED_MEDIA_HOSTS


def discover_page_delivery_videos(page: str, page_url: str) -> list[tuple[str, str]]:
    urls = discover_page_delivery_urls(page, page_url)
    return [(delivery_title(url), url) for url in urls]


def discover_page_delivery_urls(page: str, page_url: str) -> list[str]:
    decoded_page = html.unescape(page).replace("\\/", "/")
    url_pattern = re.compile(
        r"""(?:"|')?((?:https?:)?//[^"'<>\\\s]+|/[^"'<>\\\s]+)(?:"|')?""",
        re.I,
    )
    urls: list[str] = []

    for match in url_pattern.findall(decoded_page):
        candidate = clean_candidate_url(match)
        if candidate.startswith("//"):
            candidate = "https:" + candidate

        try:
            full_url = urljoin(page_url, candidate)
        except ValueError:
            continue

        if is_preferred_page_delivery_url(full_url):
            urls.append(full_url)

    urls = dedupe_preserve(urls)
    hls_urls = [url for url in urls if delivery_kind(url) == "hls"]
    return hls_urls or sorted(urls, key=delivery_sort_key)


def clean_candidate_url(value: str) -> str:
    return html.unescape(value).strip().rstrip("),.;]}")


def is_preferred_page_delivery_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    hostname = (parsed.hostname or "").lower()
    return hostname in PREFERRED_PAGE_DELIVERY_HOSTS and delivery_kind(url) is not None


def delivery_kind(url: str) -> str | None:
    match = DELIVERY_MARKER_PATTERN.search(html.unescape(url).lower())
    return match.group(1) if match else None


def delivery_sort_key(url: str) -> tuple[int, str]:
    order = {"hls": 0, "mpd": 1, "pmd": 2}
    return order.get(delivery_kind(url) or "", 3), url


def delivery_title(url: str) -> str:
    kind = delivery_kind(url)
    return f"BILD delivery {kind.upper()}" if kind else "BILD delivery"


def dedupe_videos(videos: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for title, url in videos:
        if url not in seen:
            seen.add(url)
            unique.append((title, url))
    return unique


def dedupe_preserve(values: list[str]) -> list[str]:
    """Drop duplicates while keeping first-appearance order."""
    seen: set[str] = set()
    return [v for v in values if not (v in seen or seen.add(v))]


# --- canonical embed URLs (forms yt-dlp's extractors recognize) --------------

def youtube_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def vimeo_url(video_id: str) -> str:
    return f"https://vimeo.com/{video_id}"


def brightcove_url(account_id: str, player_id: str | None, video_id: str) -> str:
    # BrightcoveNew matches players.brightcove.net/<account>/<player>_<embed>/index.html
    return (
        f"https://players.brightcove.net/{account_id}/"
        f"{player_id or 'default'}_default/index.html?videoId={video_id}"
    )


def jwplayer_url(media_id: str) -> str:
    return f"https://cdn.jwplayer.com/v2/media/{media_id}"


def kaltura_url(partner_id: str, entry_id: str) -> str:
    # yt-dlp's KalturaIE understands this compact scheme directly.
    return f"kaltura:{partner_id}:{entry_id}"


# --- page fetch + embed discovery --------------------------------------------

def fetch_text(page_url: str, deadline: Deadline | None = None) -> str:
    """Fetch text once. Returns "" (not an error) on failure."""
    timeout = deadline.remaining(cap=FETCH_BUDGET_CAP_SECONDS) if deadline else 10
    if timeout <= DEADLINE_FLOOR_SECONDS:
        return ""
    try:
        request = Request(page_url, headers={"User-Agent": BROWSER_USER_AGENT})
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        pass
    return fetch_text_with_curl(page_url, deadline)


def fetch_html(page_url: str, deadline: Deadline | None = None) -> str:
    """Fetch a page ONCE for the fallback. Returns "" (not an error) on failure.

    The fetch is capped to a short slice of the shared deadline so candidate
    probing and the generic last resort still have budget afterwards.
    """
    page = fetch_text(page_url, deadline)
    return html.unescape(page) if page else ""


def fetch_text_with_curl(page_url: str, deadline: Deadline | None = None) -> str:
    """Raw curl fetch used for JSON/JS resources that must not be HTML-unescaped."""
    timeout = deadline.remaining(cap=FETCH_BUDGET_CAP_SECONDS) if deadline else 15
    if timeout <= DEADLINE_FLOOR_SECONDS:
        return ""
    if not shutil.which("curl"):
        return ""
    command = [
        "curl",
        "-fsSL",
        "--connect-timeout",
        f"{min(5.0, timeout):.3f}",
        "--max-time",
        f"{timeout:.3f}",
        "-A",
        BROWSER_USER_AGENT,
        page_url,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=True,
            text=True,
        )
        return result.stdout or ""
    except (OSError, subprocess.CalledProcessError):
        pass
    return ""


def fetch_html_with_curl(page_url: str, deadline: Deadline | None = None) -> str:
    """Fallback for sites/environments where urllib cannot resolve or fetch.

    Single attempt, bounded by the remaining shared budget — no retry loop can
    overrun the hotkey budget. Makes NO subprocess call once the deadline has
    effectively expired.
    """
    page = fetch_text_with_curl(page_url, deadline)
    return html.unescape(page) if page else ""


def discover_json_ld_video_sources(blocks: list[str], page_url: str) -> list[tuple[str, str]]:
    videos: list[tuple[str, str]] = []
    for block in blocks:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        collect_json_ld_video_sources(data, page_url, videos)
    return videos


def collect_json_ld_video_sources(data, page_url: str, videos: list[tuple[str, str]]) -> None:
    if isinstance(data, list):
        for item in data:
            collect_json_ld_video_sources(item, page_url, videos)
        return

    if not isinstance(data, dict):
        return

    item_type = data.get("@type")
    types = item_type if isinstance(item_type, list) else [item_type]
    is_video = any(str(value).lower() == "videoobject" for value in types if value is not None)
    if is_video:
        title = str(data.get("name") or data.get("headline") or "Structured video")
        for key in ("contentUrl", "embedUrl", "url"):
            value = data.get(key)
            if isinstance(value, list):
                for nested in value:
                    add_structured_video_source(videos, page_url, nested, title, key)
            else:
                add_structured_video_source(videos, page_url, value, title, key)

    for value in data.values():
        if isinstance(value, (dict, list)):
            collect_json_ld_video_sources(value, page_url, videos)


def add_structured_video_source(
    videos: list[tuple[str, str]],
    page_url: str,
    raw_url,
    title: str,
    key: str,
) -> None:
    if not isinstance(raw_url, str):
        return
    try:
        media_url = urljoin(page_url, html.unescape(raw_url.strip()))
    except ValueError:
        return
    if looks_like_direct_media_url(media_url):
        videos.append((f"{title} ({key})", media_url))


def discover_html_media_sources(page: str, page_url: str) -> list[tuple[str, str]]:
    parser = HTMLMediaSourceParser(page_url)
    try:
        parser.feed(page)
    except Exception:
        return []
    return dedupe_videos([
        *parser.sources,
        *discover_json_ld_video_sources(parser.json_ld_blocks, page_url),
    ])


def discover_guardian_interactive_videos(
    page: str,
    page_url: str,
    deadline: Deadline,
) -> list[tuple[str, str]]:
    """Guardian documentary interactives store the playable YouTube id in docsdata."""
    document_id = guardian_documentary_id(page)
    app_urls = guardian_interactive_app_urls(page)
    if not document_id or not app_urls:
        return []

    for app_url in app_urls:
        if deadline.expired:
            return []
        script = fetch_text(app_url, deadline)
        sheet_id = guardian_sheet_id(script)
        if not sheet_id:
            continue

        if deadline.expired:
            return []
        data_url = f"https://interactive.guim.co.uk/docsdata/{sheet_id}.json"
        videos = guardian_docsdata_videos(fetch_text(data_url, deadline), page_url, document_id)
        if videos:
            return videos
    return []


def guardian_documentary_id(page: str) -> str | None:
    for tag in re.findall(r"<[^>]+>", page):
        if "docu-root" not in tag.lower():
            continue
        match = GUARDIAN_DOCU_ID.search(tag)
        if match:
            return html.unescape(match.group(1)).strip()
    return None


def guardian_interactive_app_urls(page: str) -> list[str]:
    urls: list[str] = []
    for raw_url in GUARDIAN_INTERACTIVE_APP_URL.findall(page):
        url = "https:" + raw_url if raw_url.startswith("//") else raw_url
        urls.append(url)
    return dedupe_preserve(urls)[:3]


def guardian_sheet_id(script: str) -> str | None:
    match = GUARDIAN_SHEET_ID.search(script)
    return match.group(1) if match else None


def guardian_docsdata_videos(
    raw_json: str,
    page_url: str,
    document_id: str,
) -> list[tuple[str, str]]:
    if not raw_json:
        return []
    try:
        payload = json.loads(raw_json)
    except ValueError:
        return []

    rows = payload.get("sheets", {}).get("documentaries", [])
    if not isinstance(rows, list):
        return []

    page_key = guardian_link_key(page_url)
    for row in rows:
        if not isinstance(row, dict) or not guardian_docsdata_row_matches(row, page_key, document_id):
            continue
        video_id = str(row.get("youTubeId") or "").strip()
        if not re.fullmatch(_YT_ID, video_id):
            continue
        title = str(row.get("title") or "Guardian documentary").strip()
        return [(title, youtube_watch_url(video_id))]
    return []


def guardian_link_key(url: str) -> str:
    """Normalize a Guardian URL for docsdata matching: drop query/fragment
    (share links carry ?CMP=... trackers) and any trailing slash."""
    return url.strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")


def guardian_docsdata_row_matches(row: dict, page_key: str, document_id: str) -> bool:
    if str(row.get("docName") or "").strip() == document_id:
        return True
    return guardian_link_key(str(row.get("fullLink") or "")) == page_key


def discover_embedded_urls(page: str) -> list[str]:
    """Stage 1: canonical YouTube/Vimeo URLs embedded in the page HTML."""
    urls: list[str] = []

    for pattern in EMBED_URL_PATTERNS:
        for video_id in pattern.findall(page):
            urls.append(youtube_watch_url(video_id))

    for vimeo_id in VIMEO_EMBED_PATTERN.findall(page):
        urls.append(vimeo_url(vimeo_id))

    # Only trust the non-standard data-video-id when the page clearly uses YouTube,
    # to avoid matching an unrelated 11-char token as a video id.
    if "youtube.com" in page or "youtu.be" in page:
        for video_id in DATA_VIDEO_ID_PATTERN.findall(page):
            urls.append(youtube_watch_url(video_id))

    return dedupe_preserve(urls)[:5]


def discover_player_urls(page: str) -> list[str]:
    """Stage 2: hosted-player embeds (Brightcove / JWPlayer / Kaltura)."""
    urls = brightcove_candidates(page) + jwplayer_candidates(page) + kaltura_candidates(page)
    return dedupe_preserve(urls)[:8]


def brightcove_candidates(page: str) -> list[str]:
    urls: list[str] = []

    # Ready-made player iframe/script URLs — hand straight to yt-dlp.
    for match in BRIGHTCOVE_PLAYER_URL.findall(page):
        urls.append(match)

    # <video-js>/<video> tags carrying data-account + data-video-id (+ data-player).
    for tag in BRIGHTCOVE_TAG.findall(page):
        account = BRIGHTCOVE_ACCOUNT.search(tag)
        video_id = BRIGHTCOVE_VIDEO_ID.search(tag)
        player = BRIGHTCOVE_PLAYER.search(tag)
        if account and video_id:
            urls.append(brightcove_url(account.group(1),
                                       player.group(1) if player else None,
                                       video_id.group(1)))
    return urls


def jwplayer_candidates(page: str) -> list[str]:
    media_ids = JWPLAYER_URL_ID.findall(page) + JWPLAYER_MEDIA_ID.findall(page)
    return [jwplayer_url(media_id) for media_id in media_ids]


def kaltura_candidates(page: str) -> list[str]:
    urls: list[str] = [match for match in KALTURA_PLAYER_URL.findall(page)]

    partner = KALTURA_PARTNER.search(page)
    entry = KALTURA_ENTRY.search(page)
    if partner and entry:
        urls.append(kaltura_url(partner.group(1), entry.group(1)))
    return urls


def extract_with_timeout(page_url: str, cookies_browser: str | None):
    """Run extraction on a daemon thread so we can give up cleanly.

    Returns (videos, source, error). source is 'primary'/'fallback'/'none', or
    'timeout' when the wall-clock budget is exceeded.
    """
    result: dict[str, object] = {}

    def worker():
        try:
            result["videos"], result["source"] = extract_videos_traced(page_url, cookies_browser)
        except Exception as error:  # yt-dlp raises a handful of types; treat all as "no video"
            result["error"] = error

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(EXTRACT_TIMEOUT_SECONDS)

    if thread.is_alive():
        return None, "timeout", TimeoutError(f"gave up after {EXTRACT_TIMEOUT_SECONDS}s")
    return result.get("videos"), result.get("source", "none"), result.get("error")


def short(url: str, limit: int = 60) -> str:
    clean = url.split("?", 1)[0].replace("https://", "").replace("http://", "")
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def make_html_link(url: str, text: str | None = None) -> str:
    escaped_href = html.escape(url, quote=True)
    escaped_text = html.escape(text or url)
    return f'<a href="{escaped_href}">{escaped_text}</a>'


class Args:
    def __init__(self):
        self.url = None
        self.cookies_browser = None
        self.want_html = False
        self.want_subtitle_prompt = False
        self.want_subtitle_auto = False
        self.sweep = False
        self.seed = None
        self.subtitles = None
        self.no_subtitle_prompt = False
        self.subtitle_language = "auto"
        self.subtitle_model = "small"
        self.subtitle_service_url = SUBTITLE_SERVICE_URL


def parse_args(argv: list[str]) -> Args:
    args = Args()
    # --cookies-from-browser matches yt-dlp; --cookies-from is a kept alias.
    cookie_flags = {"--cookies-from-browser", "--cookies-from"}

    i = 0
    while i < len(argv):
        arg = argv[i]
        flag, _, inline_value = arg.partition("=")

        if flag in cookie_flags:
            if inline_value:                        # --cookies-from-browser=chrome
                args.cookies_browser = inline_value
            elif i + 1 < len(argv):                 # --cookies-from-browser chrome
                args.cookies_browser = argv[i + 1]
                i += 2
                continue
            else:
                print(f"error: {flag} needs a browser name", file=sys.stderr)
                sys.exit(2)
        elif flag == "--seed":
            raw_seed = inline_value
            if not raw_seed and i + 1 < len(argv):
                raw_seed = argv[i + 1]
                i += 2
                if not raw_seed.lstrip("-").isdigit():
                    print("error: --seed needs an integer", file=sys.stderr)
                    sys.exit(2)
                args.seed = int(raw_seed)
                continue
            if not raw_seed.lstrip("-").isdigit():
                print("error: --seed needs an integer", file=sys.stderr)
                sys.exit(2)
            args.seed = int(raw_seed)
        elif flag == "--subtitles":
            if inline_value:
                args.subtitles = inline_value
            elif i + 1 < len(argv):
                args.subtitles = argv[i + 1]
                i += 2
                continue
        elif flag == "--subtitle-language":
            if inline_value:
                args.subtitle_language = inline_value
            elif i + 1 < len(argv):
                args.subtitle_language = argv[i + 1]
                i += 2
                continue
        elif flag == "--subtitle-model":
            if inline_value:
                args.subtitle_model = inline_value
            elif i + 1 < len(argv):
                args.subtitle_model = argv[i + 1]
                i += 2
                continue
        elif flag == "--subtitle-service-url":
            if inline_value:
                args.subtitle_service_url = inline_value
            elif i + 1 < len(argv):
                args.subtitle_service_url = argv[i + 1]
                i += 2
                continue
        elif arg == "--no-subtitle-prompt":
            args.no_subtitle_prompt = True
        elif arg == "--sweep-test":
            args.sweep = True
        elif arg == "--html":
            args.want_html = True
        elif arg == "--subtitle-prompt":
            args.want_subtitle_prompt = True
        elif arg == "--subtitle-auto":
            args.want_subtitle_auto = True
        elif arg in {"-h", "--help"}:
            print(__doc__)
            sys.exit(0)
        elif not arg.startswith("-") and args.url is None:
            args.url = arg
        i += 1

    if args.cookies_browser is not None:
        args.cookies_browser = validate_browser(args.cookies_browser)
    if args.subtitles is not None:
        args.subtitles = args.subtitles.strip().lower()
        if args.subtitles not in {"none", "live"}:
            print(f"error: --subtitles must be 'none' or 'live', got {args.subtitles!r}",
                  file=sys.stderr)
            sys.exit(2)
    args.subtitle_service_url = (args.subtitle_service_url or SUBTITLE_SERVICE_URL).rstrip("/")

    return args


def validate_browser(name: str) -> str:
    normalized = name.strip().lower()
    if normalized not in SUPPORTED_BROWSERS:
        supported = ", ".join(SUPPORTED_BROWSERS)
        print(f"error: unsupported browser {name!r}. Choose one of: {supported}",
              file=sys.stderr)
        sys.exit(2)
    return normalized


def run_sweep(seed: int | None) -> None:
    """Smoke-test the extractor against a random sample of public sites.

    Samples 50 news/publisher + 50 general domains from sweep_sites.py, runs each
    through the timeout-guarded extractor (no clipboard, no notifications), and
    prints a summary. Site selection is deterministic given --seed.
    """
    try:
        from sweep_sites import NEWS_SITES, GENERAL_SITES
    except ImportError:
        print("error: sweep_sites.py not found (required for --sweep-test)", file=sys.stderr)
        sys.exit(2)

    if seed is None:
        seed = random.randrange(1, 1_000_000)
    rng = random.Random(seed)

    news = rng.sample(NEWS_SITES, min(50, len(NEWS_SITES)))
    general = rng.sample(GENERAL_SITES, min(50, len(GENERAL_SITES)))
    sites = [("news", u) for u in news] + [("general", u) for u in general]

    print(f"sweep seed={seed}  sites={len(sites)} "
          f"(news={len(news)}, general={len(general)}, timeout={EXTRACT_TIMEOUT_SECONDS}s)\n",
          flush=True)

    def probe(item):
        group, url = item
        start = time.time()
        videos, source, error = extract_with_timeout(url, None)
        videos = videos or []
        return {
            "group": group,
            "url": url,
            "source": source,
            "n": len(videos),
            "primary": source == "primary" and bool(videos),
            "fallback": source == "fallback" and bool(videos),
            "first": videos[0][1] if videos else "",
            "elapsed": round(time.time() - start, 1),
            "error": type(error).__name__ if error else None,
        }

    results = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        for future in as_completed({executor.submit(probe, s): s for s in sites}):
            record = future.result()
            results.append(record)
            mark = "V" if record["n"] else ("T" if record["error"] == "TimeoutError"
                                            else "E" if record["error"] else "-")
            print(f"[{mark}] {record['elapsed']:>4}s {record['group']:<7} "
                  f"{short(record['url'], 38):<38} src={record['source']:<8} n={record['n']}",
                  flush=True)

    print_sweep_summary(results, seed)


def print_sweep_summary(results: list[dict], seed: int) -> None:
    total = len(results)
    with_video = sum(1 for r in results if r["n"] > 0)
    fast_path = sum(1 for r in results if r["source"] == "fast-path" and r["n"] > 0)
    primary = sum(1 for r in results if r["primary"])
    fallback = sum(1 for r in results if r["fallback"])
    timeouts = sum(1 for r in results if r["error"] == "TimeoutError")
    errors = sum(1 for r in results if r["error"] and r["error"] != "TimeoutError")
    no_video = sum(1 for r in results if r["n"] == 0 and not r["error"])

    print("\n" + "=" * 60)
    print(f"SWEEP SUMMARY (seed={seed})")
    print("=" * 60)
    rows = [
        ("total tested", total),
        ("videos found", with_video),
        ("  fast-path hits", fast_path),
        ("  primary hits", primary),
        ("  fallback hits", fallback),
        ("no-video", no_video),
        ("errors", errors),
        ("timeouts", timeouts),
    ]
    for label, value in rows:
        print(f"  {label:<16} {value}")

    fallback_hits = [r for r in results if r["fallback"]]
    if fallback_hits:
        print("\n  fallback resolved:")
        for r in sorted(fallback_hits, key=lambda r: r["url"]):
            print(f"    {short(r['url'], 36):<36} -> {r['first']}")

    print("\n  slowest 10:")
    for r in sorted(results, key=lambda r: -r["elapsed"])[:10]:
        tag = r["error"] or ("video" if r["n"] else "none")
        print(f"    {r['elapsed']:>5}s  {short(r['url'], 40):<40} {tag}")


def subtitle_mode_may_be_live(args) -> bool:
    """True when the flow below could still end up in live-subtitle mode."""
    if args.subtitles:
        return args.subtitles == "live"
    if args.no_subtitle_prompt:
        return False
    return args.want_subtitle_auto or args.want_subtitle_prompt


def resolve_subtitle_mode(args, title: str) -> str | None:
    if args.subtitles:
        return args.subtitles
    if args.no_subtitle_prompt:
        return "none"
    if args.want_subtitle_auto:
        return "live"
    if not args.want_subtitle_prompt:
        return "none"

    prompt = "How would you like subtitles?"
    if shutil.which("osascript"):
        # AppleScript statements are separated by REAL newlines. A literal
        # backslash-n here is an AppleScript syntax error that gets silently
        # swallowed, which disabled this dialog entirely from the hotkey.
        script = (
            f'set chosen to choose from list {{"No subtitles", "Live subtitles"}} '
            f'with prompt {applescript_string(prompt)} '
            f'with title "Video Subtitles" '
            'without multiple selections allowed\n'
            'if chosen is false then return "Cancel"\n'
            'return item 1 of chosen'
        )
        try:
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=True)
            choice = result.stdout.strip("\n")
            if choice == "No subtitles": return "none"
            if choice == "Live subtitles": return "live"
            if choice == "Cancel": return None
        except (OSError, subprocess.CalledProcessError):
            pass

    if sys.stdin.isatty():
        choice = choose_in_terminal(["No subtitles", "Live subtitles"], prompt)
        if choice == 0: return "none"
        if choice == 1: return "live"
        return None

    return "none"


def create_live_subtitle_session(media_url: str, title: str, language: str, model: str) -> tuple[dict | None, str | None]:
    # Generate the IPC socket path here (in /tmp - see make_iina_ipc_path)
    # rather than letting the server default one from its own environment.
    # The server's own tempfile.gettempdir() fallback is fine as long as the
    # server process itself isn't sandboxed, but supplying our own keeps the
    # guarantee explicit regardless of how/where the service happens to run.
    payload = json.dumps({
        "media_url": media_url,
        "title": title,
        "language": language,
        "model": model,
        "mode": "live",
        "iina_ipc_path": make_iina_ipc_path(),
    }).encode("utf-8")
    request = Request(
        f"{SUBTITLE_SERVICE_URL}/sessions",
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=SESSION_CREATE_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8")), None
    except HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8"))
            detail = body.get("detail") or error.reason
        except (OSError, ValueError):
            detail = error.reason
        return None, str(detail)
    except (URLError, TimeoutError, OSError, ValueError) as error:
        return None, str(error)


def start_live_subtitle_session(session_id: str) -> tuple[bool, str | None]:
    request = Request(
        f"{SUBTITLE_SERVICE_URL}/sessions/{session_id}/start",
        data=b"",
        headers={"Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            response.read()
            return True, None
    except HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8"))
            detail = body.get("detail") or error.reason
        except (OSError, ValueError):
            detail = error.reason
        return False, str(detail)
    except (URLError, TimeoutError, OSError, ValueError) as error:
        return False, str(error)


def stop_subtitle_session(session_id: str) -> None:
    request = Request(
        f"{SUBTITLE_SERVICE_URL}/sessions/{session_id}/stop",
        data=b"",
        headers={"Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            pass
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        pass


def ask_open_without_subtitles(error: str) -> bool:
    # Real newline in the prompt: applescript_string turns it into the "\n"
    # escape AppleScript understands, so the dialog shows two lines.
    prompt = f"Subtitle service failed: {error}\nOpen video without subtitles?"
    if shutil.which("osascript"):
        # Statements separated by REAL newlines (see resolve_subtitle_mode).
        script = (
            f'display dialog {applescript_string(prompt)} '
            'buttons {"Cancel", "Open"} default button "Open" cancel button "Cancel" '
            'with title "Video Subtitles"\n'
            'return button returned of result'
        )
        try:
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=True)
            return result.stdout.strip() == "Open"
        except (OSError, subprocess.CalledProcessError):
            pass

    if sys.stdin.isatty():
        choice = input(f"{prompt} [Y/n] ").strip().lower()
        return choice in {"y", "yes", ""}
    return False


def main() -> None:
    args = parse_args(sys.argv[1:])
    global SUBTITLE_SERVICE_URL
    SUBTITLE_SERVICE_URL = args.subtitle_service_url

    if args.sweep:
        run_sweep(args.seed)
        return

    if not args.url:
        args.url = read_clipboard().strip()

    page_url = normalize_page_url(args.url)
    if not looks_like_url(page_url):
        if sys.stdin.isatty():
            page_url = normalize_page_url(input("Enter website URL: "))
        if not looks_like_url(page_url):
            notify("Clipboard doesn't contain a URL.")
            sys.exit(1)

    videos, _source, error = extract_with_timeout(page_url, args.cookies_browser)
    mark_timing(f"extracted ({_source})")

    if not videos:
        if isinstance(error, TimeoutError):
            notify(f"Timed out on {short(page_url, 40)}")
        else:
            notify(f"No video found on {short(page_url, 40)}")
        sys.exit(1)

    if len(videos) == 1:
        title, chosen = videos[0]
    else:
        labels = [f"{title}  —  {short(url)}" for title, url in videos]
        index = choose_from_list(labels, f"{len(videos)} videos found — pick one:")
        if index is None:
            notify("Cancelled — nothing copied.")
            return
        title, chosen = videos[index]

    payload = make_html_link(chosen, title) if args.want_html else chosen
    label = "HTML link" if args.want_html else "link"

    copied = copy_to_clipboard(payload)

    # Stream resolution needs the network; the subtitle dialog needs the
    # human. Run them concurrently so picking "Live subtitles" costs almost
    # no extra wall time on top of the dialog itself.
    wait_for_streams = None
    if subtitle_mode_may_be_live(args):
        wait_for_streams = begin_stream_resolution(chosen, args.cookies_browser)

    subtitle_mode = resolve_subtitle_mode(args, title)
    if subtitle_mode is None:
        if copied:
            notify("Cancelled — link copied, but video was not opened.")
        else:
            notify("Cancelled — video was not opened.")
        return

    opened = False
    if subtitle_mode == "live":
        streams = wait_for_streams() if wait_for_streams else None
        mark_timing("streams resolved" if streams else "stream resolution failed")
        if streams:
            display_url = streams.display_url
            audio_url = streams.audio_url
            iina_audio_url = streams.iina_audio_url
        else:
            display_url = audio_url = chosen
            iina_audio_url = None
        session, session_error = create_live_subtitle_session(
            media_url=audio_url,
            title=title,
            language=args.subtitle_language,
            model=args.subtitle_model
        )
        # Guard the response shape too: a session without an id or socket
        # path is as unusable as no session, and must not crash the hotkey.
        ipc_socket_path = (session or {}).get("ipc_socket_path")
        session_id = (session or {}).get("id")
        if not ipc_socket_path or not session_id:
            detail = session_error or "service response missing session id or IPC socket path"
            if ask_open_without_subtitles(detail):
                opened = open_in_iina(chosen)
            else:
                return
        else:
            mark_timing("subtitle session created")
            opened = open_in_iina(display_url, ipc_path=ipc_socket_path, audio_url=iina_audio_url)
            mark_timing("IINA launched" if opened else "IINA launch failed")
            if opened:
                started, start_error = start_live_subtitle_session(session_id)
                mark_timing("live subtitles started")
                if not started:
                    stop_subtitle_session(session_id)
                    notify(
                        "Opened in IINA, but couldn't start live subtitles: "
                        f"{start_error or 'Unknown error'}"
                    )
                    return
            else:
                stop_subtitle_session(session_id)
    else:
        opened = open_in_iina(chosen)

    if copied and opened:
        notify(f"Copied {label} and opened in IINA: {short(chosen)}")
    elif copied:
        notify(f"Copied {label}, but couldn't open IINA: {short(chosen)}")
    elif opened:
        print(payload)
        notify(f"Opened in IINA, but couldn't reach the clipboard — printed {label}.")
    else:
        print(payload)
        notify(f"Couldn't reach the clipboard or IINA — printed {label} to terminal.")


if __name__ == "__main__":
    main()
