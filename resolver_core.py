#!/usr/bin/env python3
"""Pure resolver core shared by the videolink CLI and (later) the daemon.

Step 1 of the daemon migration plan: everything in this module is
network-free and safe to import from a long-running service. The videolink
CLI keeps fetching, probing, and all UI; this module owns the logic those
stages share — URL predicates, candidate discovery and scoring, deadline
math, stream selection, cache keys, and extraction-error classification.

Rules for this module:
  * no module-level yt_dlp import (lazy inside specific_extractor_name /
    cache_key) so importing the core stays cheap and testable offline
  * no sockets, no subprocesses, no clipboard/notification/IINA code
  * discovery functions take the page text as an argument; they never fetch
"""

from __future__ import annotations

import html
import json
import os
import platform
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, NamedTuple
from urllib.parse import parse_qs, urljoin, urlparse


def app_support_dir() -> Path:
    """Per-user data directory for VideoLink, resolved per operating system.

    macOS keeps its native ``~/Library/Application Support/VideoLink`` so the
    existing app behaves exactly as before. Everywhere else we follow the XDG
    base-directory spec (``$XDG_DATA_HOME`` or ``~/.local/share``). The
    ``VIDEOLINK_APP_SUPPORT_DIR`` override wins on every platform so tests and
    the installer can point the service and CLI at a scratch location.

    Pure path math only — no filesystem or network side effects — so it stays
    safe to import from the resolver core and the long-running service alike.
    """
    override = os.environ.get("VIDEOLINK_APP_SUPPORT_DIR")
    if override:
        return Path(override).expanduser()
    if platform.system() == "Darwin":
        return Path.home() / "Library/Application Support/VideoLink"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    xdg_path = Path(xdg_data_home).expanduser() if xdg_data_home else None
    # The XDG spec requires an absolute path. Ignore malformed relative values
    # instead of letting the service's data directory depend on its cwd.
    base = xdg_path if xdg_path and xdg_path.is_absolute() else Path.home() / ".local/share"
    return base / "VideoLink"

# Used only by the fallback: some publishers wrap a YouTube or Vimeo video in
# their own player, so yt-dlp's generic extractor sees no embed. These find
# the underlying video id, which the caller then resolves through yt-dlp.
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

EMBED_TAG_SRC = re.compile(
    r"<(?:iframe|embed|object)\b[^>]*?\s(?:src|data)\s*=\s*[\"']([^\"']+)[\"']", re.I
)

# Embedded-player candidates probed at once; first usable result wins.
PROBE_CANDIDATE_LIMIT = 8
# Extractors that claim playlist/channel/search/collection pages. Those still
# go through the normal flow so the multi-video picker keeps working.
FAST_PATH_EXCLUDED_MARKERS = (
    "playlist", "tab", "channel", "user", "search", "feed", "album", ":set",
)
# Extractors whose page flow must always run even for a single video, because
# their resolved media needs post-processing (e.g. BILD's fragile CDN URLs are
# swapped for durable hds.ak.token.bild.de delivery manifests).
FAST_PATH_DENYLISTED_EXTRACTORS = {"bild"}


# --- deadline: one monotonic budget shared by every stage ----------------------

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


# --- offline extractor matching -------------------------------------------------

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


# --- URL predicates and helpers --------------------------------------------------

def normalize_page_url(raw_url: str) -> str:
    page_url = raw_url.strip()
    if page_url and not urlparse(page_url).scheme:
        page_url = f"https://{page_url.lstrip('/')}"
    # Search result links should resolve the publisher, not Google's redirect
    # HTML/consent page. Restrict unwrapping to known hosts and HTTP targets.
    google_hosts = {"google.com", "www.google.com", "google.de", "www.google.de",
                    "google.co.uk", "www.google.co.uk"}
    seen = {page_url}
    for _ in range(5):
        parsed = urlparse(page_url)
        if parsed.hostname not in google_hosts or parsed.path != "/url":
            break
        query = parse_qs(parsed.query)
        target = next((value for key in ("url", "q") for value in query.get(key, [])
                       if looks_like_url(value)), None)
        if target is None or target in seen:
            break
        seen.add(target)
        page_url = target
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
        or delivery_kind(parsed.path) is not None
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


def clean_candidate_url(value: str) -> str:
    return html.unescape(value).strip().rstrip("),.;]}")


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


# --- scored candidates ------------------------------------------------------------

# Confidence tiers, highest first. Ordering bugs (an og:video preview outranking
# the real YouTube embed) are impossible by construction: candidates are sorted
# by tier, and ties keep discovery order.
CONFIDENCE = {
    "canonical_embed": 80,   # YouTube/Vimeo id lifted straight from the page
    "platform_embed": 80,    # iframe/embed/object src claimed by a specific extractor
    "jsonld_platform": 70,   # VideoObject embedUrl/contentUrl claimed by an extractor
    "hosted_player": 60,     # Brightcove / JWPlayer / Kaltura reconstruction
    "direct_file": 50,       # <video>/<source> src or JSON-LD direct media
    "og_video": 30,          # og:video / twitter:player — often a low-res preview
}


@dataclass(frozen=True)
class Candidate:
    url: str
    kind: str
    title: str = ""
    # needs_probe: hand to yt-dlp to resolve. False means the URL is already
    # a playable media URL and probing would only spend budget.
    needs_probe: bool = True

    @property
    def confidence(self) -> int:
        return CONFIDENCE[self.kind]


def discover_scored_candidates(
    page: str,
    page_url: str,
    match_extractor: Callable[[str], str | None] | None = None,
) -> list[Candidate]:
    """Every discovery shape, deduped and sorted by confidence tier.

    The daemon-facing API. The CLI's staged flow consumes projections of this
    list (probe-able candidates for the yt-dlp race, direct ones as terminal
    fallback results) so both paths share one discovery implementation.
    """
    match = match_extractor or specific_extractor_name
    candidates: list[Candidate] = []

    for url in discover_embedded_urls(page):
        candidates.append(Candidate(url, "canonical_embed"))

    for tag_match in EMBED_TAG_SRC.finditer(page):
        try:
            candidate = urljoin(page_url, html.unescape(tag_match.group(1)).strip())
        except ValueError:
            continue
        if candidate.startswith(("http://", "https://")) and match(candidate):
            candidates.append(Candidate(candidate, "platform_embed"))

    parser = HTMLMediaSourceParser(page_url)
    try:
        parser.feed(page)
    except Exception:
        parser = None

    if parser is not None:
        for url, title in json_ld_video_urls(parser.json_ld_blocks, page_url):
            if looks_like_direct_media_url(url):
                candidates.append(Candidate(url, "direct_file", title, needs_probe=False))
            elif match(url):
                candidates.append(Candidate(url, "jsonld_platform", title))

    for url in discover_player_urls(page):
        candidates.append(Candidate(url, "hosted_player"))

    if parser is not None:
        for title, url in parser.sources:
            kind = "og_video" if title.startswith("Direct media og:") or "twitter:player" in title else "direct_file"
            candidates.append(Candidate(url, kind, title, needs_probe=False))

    seen: set[str] = set()
    unique = [c for c in candidates if not (c.url in seen or seen.add(c.url))]
    # sorted() is stable: equal tiers keep first-appearance (discovery) order.
    return sorted(unique, key=lambda c: -c.confidence)


def discover_embed_candidate_urls(
    page: str,
    page_url: str,
    match_extractor: Callable[[str], str | None] | None = None,
) -> list[str]:
    """Embedded-player candidate URLs for the yt-dlp probe race, best first.

    Projection of discover_scored_candidates: only candidates that need a
    probe, capped so a tag-soup page can't fan out unbounded work.
    """
    scored = discover_scored_candidates(page, page_url, match_extractor)
    urls = [candidate.url for candidate in scored if candidate.needs_probe]
    return urls[:PROBE_CANDIDATE_LIMIT]


def discover_embedded_urls(page: str) -> list[str]:
    """Canonical YouTube/Vimeo URLs embedded in the page HTML."""
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
    """Hosted-player embeds (Brightcove / JWPlayer / Kaltura)."""
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


# --- HTML media / JSON-LD discovery ----------------------------------------------

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


def json_ld_video_urls(blocks: list[str], page_url: str) -> list[tuple[str, str]]:
    """(url, title) for every VideoObject URL — platform links included.

    Unlike discover_json_ld_video_sources (direct media only, kept for the
    CLI's terminal fallback stage), this accepts any http(s) URL so a
    VideoObject.embedUrl pointing at YouTube — a very common publisher
    pattern — can enter the probe race instead of being discarded.
    """
    found: list[tuple[str, str]] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if any(str(value).lower() == "videoobject" for value in types if value is not None):
            title = str(node.get("name") or node.get("headline") or "")
            for key in ("contentUrl", "embedUrl", "url"):
                value = node.get(key)
                for raw in (value if isinstance(value, list) else [value]):
                    if not isinstance(raw, str):
                        continue
                    try:
                        url = urljoin(page_url, html.unescape(raw.strip()))
                    except ValueError:
                        continue
                    if url.startswith(("http://", "https://")):
                        found.append((url, title))
        for value in node.values():
            if isinstance(value, (dict, list)):
                walk(value)

    for block in blocks:
        try:
            walk(json.loads(block))
        except ValueError:
            continue
    return found


# --- BILD delivery post-processing -----------------------------------------------

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


# --- Guardian interactive documentaries (pure helpers; the fetch stays in the CLI)

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


# --- stream selection --------------------------------------------------------------

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


# --- cache keys ----------------------------------------------------------------------

def cache_key(url: str, ytdlp_version: str | None = None) -> str:
    """Stable cache key for a resolver result.

    The yt-dlp version is part of the key so a daemon that outlives an
    upgrade can never serve results produced by an older extractor registry.
    """
    if ytdlp_version is None:
        from yt_dlp.version import __version__ as ytdlp_version
    return f"{ytdlp_version}|{normalize_page_url(url)}"


# --- extraction error classification ---------------------------------------------------

# Checked in order; first hit wins. Auth problems outrank geo/paywall because
# their fix (--cookies-from-browser, or a daemon with warm cookies) is the one
# the user can act on immediately.
_ERROR_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("login_wall", (
        "login", "log in", "sign in", "signin", "cookies", "private",
        "members-only", "members only", "403", "forbidden", "unauthorized",
        "age-restricted", "confirm your age", "authentication",
    )),
    ("geo_blocked", (
        "geo", "not available in your country", "unavailable in your region",
        "region", "country",
    )),
    ("paywall", ("premium", "paywall", "subscriber", "subscription")),
    ("timeout", ("timed out", "timeout")),
    ("no_video", ("unsupported url", "no video", "no media found")),
)


def classify_extraction_error(message: str | None) -> str:
    """Coarse class for a yt-dlp extraction error: what should the user do?

    Returns one of: login_wall, geo_blocked, paywall, timeout, no_video,
    unknown. Meant for notifications ("needs browser cookies") and for the
    daemon's negative cache, never for control flow inside extraction.
    """
    if not message:
        return "unknown"
    lowered = message.lower()
    for label, markers in _ERROR_CLASSES:
        if any(marker in lowered for marker in markers):
            return label
    return "unknown"
