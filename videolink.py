#!/usr/bin/env python3
"""Grab a video link from a web page and copy it to the clipboard.

Keybind flow (macOS): copy a page URL, hit your hotkey. This reads the URL
from the clipboard, asks yt-dlp what videos are on the page, copies the link
(or shows a picker if there are several), and posts a notification.

Usage:
    videolink.py [url] [--cookies-from-browser BROWSER] [--html]

If no url is given it reads one from the clipboard.
--cookies-from-browser chrome|safari|firefox|edge|brave|chromium|opera|vivaldi
    unlocks auth-gated sites (reddit, instagram, private posts) by borrowing your
    browser's login cookies. (--cookies-from is kept as an alias.)
--html  copy an <a href> anchor tag instead of the plain URL (for rich-text
    targets like email or a CMS).
--sweep-test [--seed N]  developer smoke test: run the extractor against a random
    sample of 50 news + 50 general public sites and print a summary. Copies
    nothing. --seed makes the site selection reproducible.
"""

from __future__ import annotations

import re
import sys
import html
import time
import random
import shutil
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
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

# yt-dlp can read cookies from these browsers. We validate against this list so a
# typo fails loudly instead of silently extracting without authentication.
SUPPORTED_BROWSERS = (
    "chrome", "safari", "firefox", "edge", "brave", "chromium", "opera", "vivaldi",
)

# A web page that pulls in lots of requests can take a long time. A keybind
# tool must never hang, so we abandon extraction after this many seconds.
EXTRACT_TIMEOUT_SECONDS = 12


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
    """Quote a Python string as an AppleScript string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# --- core --------------------------------------------------------------------

def normalize_page_url(raw_url: str) -> str:
    page_url = raw_url.strip()
    if page_url and not urlparse(page_url).scheme:
        return f"https://{page_url.lstrip('/')}"
    return page_url


def looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(parsed.scheme in {"http", "https"} and parsed.netloc and "." in parsed.netloc)


def extract_videos(page_url: str, cookies_browser: str | None) -> list[tuple[str, str]]:
    """Return (title, url) for each video on the page (see extract_videos_traced)."""
    return extract_videos_traced(page_url, cookies_browser)[0]


def extract_videos_traced(
    page_url: str, cookies_browser: str | None
) -> tuple[list[tuple[str, str]], str]:
    """Return (videos, source) where source is 'primary', 'fallback', or 'none'.

    1. Ask yt-dlp directly (primary).
    2. If that finds nothing, fetch the page once and try, in order:
         a. embedded YouTube/Vimeo ids, then
         b. hosted players (Brightcove / JWPlayer / Kaltura),
       resolving every discovered candidate *through* yt-dlp. The first stage that
       yields a video wins. Candidates are deduped before probing and final
       results are deduped after.
    """
    videos = ydl_extract(page_url, cookies_browser)
    if videos:
        return videos, "primary"

    page = fetch_html(page_url)
    if page:
        for discover in (discover_embedded_urls, discover_player_urls):
            found: list[tuple[str, str]] = []
            for candidate_url in discover(page):
                found.extend(ydl_extract(candidate_url, cookies_browser))
            if found:
                return dedupe_videos(found), "fallback"

    return [], "none"


def ydl_extract(target_url: str, cookies_browser: str | None) -> list[tuple[str, str]]:
    """Resolve a single URL through yt-dlp. Returns [] if it finds nothing."""
    opts = dict(YDL_OPTS)
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.sanitize_info(ydl.extract_info(target_url, download=False))
    except yt_dlp.utils.DownloadError:
        return []  # unsupported / no video on this URL — let the caller try a fallback

    entries = info.get("entries")
    items = list(entries) if entries is not None else [info]

    videos: list[tuple[str, str]] = []
    for item in items:
        if not item:
            continue
        # webpage_url is durable and always present when we found something;
        # direct format URLs aren't always available and tend to expire.
        url = item.get("webpage_url") or item.get("url")
        if url:
            videos.append((item.get("title") or url, url))
    return dedupe_videos(videos)


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

def fetch_html(page_url: str) -> str:
    """Fetch a page once for the fallback. Returns "" (not an error) on failure."""
    try:
        request = Request(page_url, headers={"User-Agent": BROWSER_USER_AGENT})
        with urlopen(request, timeout=10) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return html.unescape(response.read().decode(charset, errors="replace"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return ""


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


def parse_args(argv: list[str]):
    url = None
    cookies_browser = None
    want_html = False
    sweep = False
    seed = None
    # --cookies-from-browser matches yt-dlp; --cookies-from is a kept alias.
    cookie_flags = {"--cookies-from-browser", "--cookies-from"}

    i = 0
    while i < len(argv):
        arg = argv[i]
        flag, _, inline_value = arg.partition("=")

        if flag in cookie_flags:
            if inline_value:                        # --cookies-from-browser=chrome
                cookies_browser = inline_value
            elif i + 1 < len(argv):                 # --cookies-from-browser chrome
                cookies_browser = argv[i + 1]
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
                seed = int(raw_seed)
                continue
            if not raw_seed.lstrip("-").isdigit():
                print("error: --seed needs an integer", file=sys.stderr)
                sys.exit(2)
            seed = int(raw_seed)
        elif arg == "--sweep-test":
            sweep = True
        elif arg == "--html":
            want_html = True
        elif arg in {"-h", "--help"}:
            print(__doc__)
            sys.exit(0)
        elif not arg.startswith("-") and url is None:
            url = arg
        i += 1

    if cookies_browser is not None:
        cookies_browser = validate_browser(cookies_browser)

    return url, cookies_browser, want_html, sweep, seed


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


def main() -> None:
    raw_url, cookies_browser, want_html, sweep, seed = parse_args(sys.argv[1:])

    if sweep:
        run_sweep(seed)
        return

    if not raw_url:
        raw_url = read_clipboard().strip()

    page_url = normalize_page_url(raw_url)
    if not looks_like_url(page_url):
        # Only prompt when there's a real terminal — under a hotkey there's no
        # TTY and input() would raise EOFError with no notification.
        if sys.stdin.isatty():
            page_url = normalize_page_url(input("Enter website URL: "))
        if not looks_like_url(page_url):
            notify("Clipboard doesn't contain a URL.")
            sys.exit(1)

    videos, _source, error = extract_with_timeout(page_url, cookies_browser)

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

    payload = make_html_link(chosen, title) if want_html else chosen
    label = "HTML link" if want_html else "link"

    if copy_to_clipboard(payload):
        notify(f"Copied {label}: {short(chosen)}")
    else:
        print(payload)
        notify(f"Couldn't reach the clipboard — printed {label} to terminal.")


if __name__ == "__main__":
    main()
