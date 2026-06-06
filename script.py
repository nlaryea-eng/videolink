#!/usr/bin/env python3

from __future__ import annotations

import json
import re
import sys
import html
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

VIDEO_EXTENSIONS = (
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
VIDEO_EXTENSION_PATTERN = re.compile(
    rf"({'|'.join(re.escape(extension) for extension in VIDEO_EXTENSIONS)})(?:$|[?#&=/])",
    re.IGNORECASE,
)

JWPLAYER_MEDIA_ID_PATTERN = re.compile(
    r"""cdn\.jwplayer\.com/(?:manifests|previews|players|v2/media)/([A-Za-z0-9]{8})""",
    re.IGNORECASE,
)
JWPLAYER_JSON_MEDIA_ID_PATTERN = re.compile(
    r'''"mediaid"\s*:\s*"([A-Za-z0-9]{8})"''',
    re.IGNORECASE,
)
TRUSTED_STRUCTURED_VIDEO_URL_PATTERNS = (
    re.compile(r'''"hls"\s*:\s*"([^"]+)"''', re.IGNORECASE),
    re.compile(
        r'''"url"\s*:\s*"([^"]+)"\s*,\s*"type"\s*:\s*"video/[^"]+"''',
        re.IGNORECASE,
    ),
    re.compile(
        r'''"type"\s*:\s*"video/[^"]+"\s*,\s*"url"\s*:\s*"([^"]+)"''',
        re.IGNORECASE,
    ),
)
CONTENT_URL_PATTERN = re.compile(r'''"contentUrl"\s*:\s*"([^"]+)"''', re.IGNORECASE)
YOUTUBE_ID_PATTERNS = (
    re.compile(r'''"assetId"\s*:\s*"([A-Za-z0-9_-]{11})"''', re.IGNORECASE),
    re.compile(r'''data-video-id=["']([A-Za-z0-9_-]{11})["']''', re.IGNORECASE),
    re.compile(
        r'''(?:youtube\.com/(?:embed/|watch\?v=)|youtu\.be/)([A-Za-z0-9_-]{11})''',
        re.IGNORECASE,
    ),
)


def looks_like_video_url(url: str) -> bool:
    normalized_url = html.unescape(url).lower()

    if any(character in normalized_url for character in "\"'<>"):
        return False

    try:
        parsed = urlparse(normalized_url)
    except ValueError:
        return False

    path = parsed.path

    if path.endswith(VIDEO_EXTENSIONS):
        return True

    # Some URLs carry the video filename in a query parameter.
    return bool(VIDEO_EXTENSION_PATTERN.search(normalized_url))


def looks_like_video_delivery_url(url: str) -> bool:
    normalized_url = html.unescape(url).lower()

    return (
        looks_like_video_url(normalized_url)
        or "delivery=hls" in normalized_url
        or "delivery=mpd" in normalized_url
    )


def is_youtube_watch_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    return is_parsed_youtube_watch_url(parsed)


def is_parsed_youtube_watch_url(parsed) -> bool:
    hostname = (parsed.hostname or "").lower()

    if hostname not in {"youtube.com", "www.youtube.com"}:
        return False

    if parsed.path != "/watch":
        return False

    return bool(parse_qs(parsed.query).get("v"))


def normalize_page_url(raw_url: str) -> str:
    page_url = raw_url.strip()

    if page_url and not urlparse(page_url).scheme:
        return f"https://{page_url.lstrip('/')}"

    return page_url


def build_request(
    url: str,
    extra_headers: dict[str, str] | None = None,
    method: str | None = None,
) -> Request:
    headers = dict(DEFAULT_HEADERS)

    if extra_headers:
        headers.update(extra_headers)

    return Request(url, headers=headers, method=method)


def clean_candidate_url(value: str) -> str:
    value = html.unescape(value).strip()

    # Broad JS/JSON URL regexes can capture trailing punctuation from the
    # surrounding syntax. Keep URL-valid query delimiters, trim closing syntax.
    return value.rstrip("),.;]}")


def absolute_candidate_url(page_url: str, value: str) -> str | None:
    candidate = clean_candidate_url(value)

    if not candidate:
        return None

    if candidate.startswith("//"):
        candidate = "https:" + candidate

    try:
        return urljoin(page_url, candidate)
    except ValueError:
        return None


class VideoLinkParser(HTMLParser):
    def __init__(self, page_url: str):
        super().__init__()
        self.page_url = page_url
        self.links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_by_name = dict(attrs)

        if tag in {"video", "audio", "source"}:
            self.add_candidate(attrs_by_name.get("src"))

        if tag == "a":
            self.add_candidate(attrs_by_name.get("href"))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def add_candidate(self, value: str | None) -> None:
        if not value:
            return

        full_url = absolute_candidate_url(self.page_url, value)

        if full_url and looks_like_video_url(full_url):
            self.links.add(full_url)


def fetch_page(page_url: str) -> str:
    request = build_request(page_url)

    with urlopen(request, timeout=20) as response:
        body = response.read()
        charset = response.headers.get_content_charset() or "utf-8"

    return body.decode(charset, errors="replace")


def extract_jwplayer_media_ids(text: str) -> set[str]:
    return set(JWPLAYER_MEDIA_ID_PATTERN.findall(text)) | set(
        JWPLAYER_JSON_MEDIA_ID_PATTERN.findall(text)
    )


def decode_json_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.replace("\\/", "/")


def extract_structured_video_links(text: str, page_url: str) -> set[str]:
    links = set()

    for pattern in TRUSTED_STRUCTURED_VIDEO_URL_PATTERNS:
        for match in pattern.findall(text):
            full_url = absolute_candidate_url(page_url, decode_json_string(match))

            if full_url:
                links.add(full_url)

    for match in CONTENT_URL_PATTERN.findall(text):
        full_url = absolute_candidate_url(page_url, decode_json_string(match))

        if full_url and looks_like_video_delivery_url(full_url):
            links.add(full_url)

    return links


def extract_youtube_links(text: str) -> set[str]:
    decoded_text = html.unescape(text)
    video_ids = set()

    for pattern in YOUTUBE_ID_PATTERNS:
        video_ids.update(pattern.findall(decoded_text))

    return {f"https://www.youtube.com/watch?v={video_id}" for video_id in video_ids}


def fetch_jwplayer_video_links(media_id: str) -> set[str]:
    api_url = f"https://cdn.jwplayer.com/v2/media/{media_id}"

    try:
        media_data = json.loads(fetch_page(api_url))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return set()

    links = set()

    for item in media_data.get("playlist", []):
        for source in item.get("sources", []):
            file_url = source.get("file")

            if file_url and looks_like_video_url(file_url):
                links.add(file_url)

    return links


def extract_video_links(page_url: str):
    html_text = fetch_page(page_url)
    parser = VideoLinkParser(page_url)
    parser.feed(html_text)

    found = set(parser.links)
    found.update(extract_structured_video_links(html_text, page_url))
    found.update(extract_youtube_links(html_text))

    # Extract URLs embedded in scripts or inline page text.
    decoded_html_text = html.unescape(html_text).replace("\\/", "/")
    url_pattern = re.compile(
        r"""(?:"|')?((?:https?:)?//[^"'<>\\\s]+|/[^"'<>\\\s]+)(?:"|')?""",
        re.IGNORECASE,
    )

    for match in url_pattern.findall(decoded_html_text):
        full_url = absolute_candidate_url(page_url, match)

        if full_url and looks_like_video_url(full_url):
            found.add(full_url)

    for media_id in extract_jwplayer_media_ids(html_text + "\n" + "\n".join(found)):
        found.update(fetch_jwplayer_video_links(media_id))

    return sorted(found)


def is_reachable_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    scheme = parsed.scheme

    if scheme not in {"http", "https"}:
        return True

    if is_parsed_youtube_watch_url(parsed):
        return True

    try:
        request = build_request(url, method="HEAD")

        with urlopen(request, timeout=10) as response:
            return 200 <= response.status < 400
    except HTTPError as e:
        if e.code in {404, 410}:
            return False
    except (URLError, TimeoutError, OSError, ValueError):
        pass

    try:
        request = build_request(url, {"Range": "bytes=0-0"})

        with urlopen(request, timeout=10) as response:
            response.read(1)
            return 200 <= response.status < 400
    except HTTPError as e:
        return e.code not in {404, 410}
    except (URLError, TimeoutError, OSError, ValueError):
        return False


def filter_reachable_urls(urls: list[str]) -> tuple[list[str], int]:
    if len(urls) <= 1:
        reachable = [url for url in urls if is_reachable_url(url)]
        return reachable, len(urls) - len(reachable)

    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as executor:
        reachable_flags = list(executor.map(is_reachable_url, urls))

    reachable = [url for url, is_reachable in zip(urls, reachable_flags) if is_reachable]

    return reachable, len(urls) - len(reachable)


def copy_to_clipboard(text: str) -> bool:
    commands = [
        ["pbcopy"],
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
        ["clip"],
    ]

    for command in commands:
        if shutil.which(command[0]):
            try:
                subprocess.run(command, input=text, text=True, check=True)
                return True
            except (OSError, subprocess.CalledProcessError):
                continue

    return False


def make_html_link(url: str) -> str:
    escaped_href = html.escape(url, quote=True)
    escaped_text = html.escape(url)

    return f'<a href="{escaped_href}">{escaped_text}</a>'


def letter_for_index(index: int) -> str | None:
    if index >= 26:
        return None

    return chr(ord("A") + index)


def index_from_choice(choice: str, total: int) -> int | None:
    normalized = choice.strip().lower()

    if normalized.isdigit():
        index = int(normalized) - 1
        if 0 <= index < total:
            return index

    if len(normalized) == 1 and "a" <= normalized <= "z":
        index = ord(normalized) - ord("a")
        if 0 <= index < min(total, 26):
            return index

    return None


def choose_video_link(video_links: list[str]) -> str | None:
    print("Found video links:\n")

    for index, url in enumerate(video_links):
        letter = letter_for_index(index)
        label = f"{index + 1}/{letter}" if letter else str(index + 1)
        print(f"{label}. {url}")

    print()

    while True:
        choice = input("Choose a URL by number or letter, or q to quit: ")

        if choice.strip().lower() in {"q", "quit", "exit"}:
            return None

        selected_index = index_from_choice(choice, len(video_links))

        if selected_index is not None:
            return video_links[selected_index]

        print("Invalid choice. Try again.")


def main():
    if len(sys.argv) > 2:
        print(f"Usage: {sys.argv[0]} [website-url]")
        sys.exit(1)

    raw_url = sys.argv[1] if len(sys.argv) == 2 else input("Enter website URL: ")
    page_url = normalize_page_url(raw_url)

    if not page_url:
        print("No URL entered.")
        sys.exit(1)

    try:
        video_links = extract_video_links(page_url)

        if not video_links:
            print("No video links found.")
            return

        print("Checking video links...")
        video_links, skipped_count = filter_reachable_urls(video_links)

        if skipped_count:
            print(f"Skipped {skipped_count} unreachable video link(s).")

        if not video_links:
            print("No reachable video links found.")
            return

        selected_url = choose_video_link(video_links)

        if selected_url is None:
            print("No URL copied.")
            return

        html_link = make_html_link(selected_url)

        if copy_to_clipboard(html_link):
            print(f"Copied HTML link to clipboard:\n{html_link}")
        else:
            print("Could not find a clipboard command. Selected HTML link:")
            print(html_link)

    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as e:
        print(f"Request failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
