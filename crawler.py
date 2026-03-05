#!/usr/bin/env python3
# requirements.txt snippet:
# requests>=2.31.0
# beautifulsoup4>=4.12.0

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urldefrag
from urllib import robotparser

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOGGER = logging.getLogger("crawler")
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v", ".avi"}
BLOCKED_STREAM_EXTENSIONS = {".m3u8", ".mpd"}
CSS_BG_URL_RE = re.compile(r"background(?:-image)?\s*:\s*[^;]*url\((['\"]?)(.*?)\1\)", re.IGNORECASE)


@dataclass
class CrawlConfig:
    start_url: str
    crawl_type: str
    out_dir: Path
    depth: int
    max_pages: int
    timeout: int
    delay: float
    headers: dict[str, str]
    user_agent: str


@dataclass
class CrawlStats:
    pages_visited: int = 0
    files_saved: int = 0
    urls_skipped: int = 0


class RobotsManager:
    def __init__(self, user_agent: str, timeout: int) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self._parsers: dict[str, robotparser.RobotFileParser | None] = {}

    def can_fetch(self, url: str) -> bool:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return False

        base = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._parsers.get(base)
        if base not in self._parsers:
            robots_url = f"{base}/robots.txt"
            parser = robotparser.RobotFileParser()
            parser.set_url(robots_url)
            try:
                parser.read()
            except Exception as exc:
                LOGGER.warning("Failed to read robots.txt (%s): %s. Defaulting to allow.", robots_url, exc)
                parser = None
            self._parsers[base] = parser

        if parser is None:
            return True

        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception as exc:
            LOGGER.warning("robots.txt parse issue for %s: %s. Defaulting to allow.", url, exc)
            return True


def build_session(headers: dict[str, str], timeout: int) -> requests.Session:
    session = requests.Session()
    session.headers.update(headers)

    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"HEAD", "GET"}),
        backoff_factor=1.0,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    # attach timeout as attribute for convenience
    session.request_timeout = timeout  # type: ignore[attr-defined]
    return session


def normalize_url(candidate: str, base_url: str) -> str | None:
    joined = urljoin(base_url, candidate)
    joined, _ = urldefrag(joined)
    parsed = urlparse(joined)
    if parsed.scheme not in {"http", "https"}:
        return None
    return joined


def is_same_domain(url_a: str, url_b: str) -> bool:
    return urlparse(url_a).netloc.lower() == urlparse(url_b).netloc.lower()


def safe_filename(name: str, default: str = "file") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned or default


def choose_extension(url: str, content_type: str | None, fallback: str = ".bin") -> str:
    path_ext = Path(urlparse(url).path).suffix.lower()
    if path_ext:
        return path_ext
    if content_type:
        guess = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guess:
            return guess
    return fallback


def hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def fetch_url(session: requests.Session, url: str, timeout: int) -> requests.Response | None:
    try:
        response = session.get(url, timeout=timeout)
    except requests.RequestException as exc:
        LOGGER.warning("Failed fetching %s: %s", url, exc)
        return None

    if response.status_code >= 400:
        LOGGER.warning("HTTP %s for %s", response.status_code, url)
        return None
    return response


def extract_links(soup: BeautifulSoup, base_url: str) -> set[str]:
    links: set[str] = set()
    for tag in soup.find_all("a", href=True):
        normalized = normalize_url(tag["href"], base_url)
        if normalized:
            links.add(normalized)
    return links


def extract_image_urls(soup: BeautifulSoup, page_url: str) -> set[str]:
    image_urls: set[str] = set()

    for tag in soup.find_all("img"):
        for attr in ("src", "data-src"):
            value = tag.get(attr)
            if value:
                normalized = normalize_url(value, page_url)
                if normalized:
                    image_urls.add(normalized)

    for source in soup.find_all("source"):
        srcset = source.get("srcset")
        if srcset:
            for part in srcset.split(","):
                token = part.strip().split(" ")[0]
                normalized = normalize_url(token, page_url)
                if normalized:
                    image_urls.add(normalized)

    for tag in soup.find_all(style=True):
        style = tag.get("style") or ""
        for _, match in CSS_BG_URL_RE.findall(style):
            normalized = normalize_url(match, page_url)
            if normalized:
                image_urls.add(normalized)

    return image_urls


def extract_video_urls(soup: BeautifulSoup, page_url: str) -> set[str]:
    video_urls: set[str] = set()

    for video in soup.find_all("video"):
        src = video.get("src")
        if src:
            normalized = normalize_url(src, page_url)
            if normalized:
                video_urls.add(normalized)

    for source in soup.find_all("source", src=True):
        normalized = normalize_url(source["src"], page_url)
        if normalized:
            video_urls.add(normalized)

    for anchor in soup.find_all("a", href=True):
        normalized = normalize_url(anchor["href"], page_url)
        if normalized:
            video_urls.add(normalized)

    return {
        u
        for u in video_urls
        if Path(urlparse(u).path).suffix.lower() not in BLOCKED_STREAM_EXTENSIONS
        and Path(urlparse(u).path).suffix.lower() in VIDEO_EXTENSIONS
    }


def extract_text_content(soup: BeautifulSoup) -> tuple[str, str]:
    title = (soup.title.string or "untitled").strip() if soup.title else "untitled"

    for tag_name in ["script", "style", "nav", "footer", "noscript", "header", "aside"]:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    main_container = soup.find("main") or soup.find("article") or soup.body or soup
    text = "\n".join(line.strip() for line in main_container.get_text("\n").splitlines() if line.strip())
    return title, text


def ensure_output_dirs(base: Path) -> dict[str, Path]:
    dirs = {
        "images": base / "images",
        "videos": base / "videos",
        "text": base / "text",
        "logs": base / "logs",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def save_binary_file(
    session: requests.Session,
    file_url: str,
    out_dir: Path,
    timeout: int,
    seen_hashes: set[str],
    index: int,
) -> bool:
    response = fetch_url(session, file_url, timeout)
    if response is None:
        return False

    content = response.content
    if not content:
        LOGGER.debug("Empty content for %s", file_url)
        return False

    digest = hash_bytes(content)
    if digest in seen_hashes:
        LOGGER.debug("Skipped duplicate content: %s", file_url)
        return False
    seen_hashes.add(digest)

    extension = choose_extension(file_url, response.headers.get("Content-Type"), fallback=".bin")
    stem = safe_filename(Path(urlparse(file_url).path).stem or f"file_{index}")
    filename = f"{stem}_{digest[:8]}{extension}"
    destination = out_dir / filename

    try:
        destination.write_bytes(content)
    except OSError as exc:
        LOGGER.warning("Failed saving %s: %s", destination, exc)
        return False

    LOGGER.info("Saved file: %s", destination)
    return True


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def crawl(config: CrawlConfig) -> CrawlStats:
    stats = CrawlStats()
    dirs = ensure_output_dirs(config.out_dir)
    jsonl_path = dirs["text"] / "pages.jsonl"

    session = build_session(config.headers, config.timeout)
    robots = RobotsManager(config.user_agent, config.timeout)

    queue: deque[tuple[str, int]] = deque([(config.start_url, 0)])
    visited: set[str] = set()
    seen_download_urls: set[str] = set()
    seen_content_hashes: set[str] = set()

    while queue and stats.pages_visited < config.max_pages:
        current_url, current_depth = queue.popleft()

        if current_url in visited:
            continue
        visited.add(current_url)

        if not robots.can_fetch(current_url):
            stats.urls_skipped += 1
            LOGGER.info("Skipped by robots.txt: %s", current_url)
            continue

        response = fetch_url(session, current_url, config.timeout)
        if response is None:
            stats.urls_skipped += 1
            continue

        content_type = response.headers.get("Content-Type", "")
        if "text/html" not in content_type:
            stats.urls_skipped += 1
            LOGGER.debug("Skipping non-HTML page: %s (%s)", current_url, content_type)
            continue

        stats.pages_visited += 1
        soup = BeautifulSoup(response.text, "html.parser")

        if config.crawl_type == "images":
            candidates = extract_image_urls(soup, current_url)
            for file_url in candidates:
                if file_url in seen_download_urls:
                    continue
                seen_download_urls.add(file_url)
                if not robots.can_fetch(file_url):
                    LOGGER.debug("Image blocked by robots.txt: %s", file_url)
                    stats.urls_skipped += 1
                    continue
                if save_binary_file(session, file_url, dirs["images"], config.timeout, seen_content_hashes, stats.files_saved + 1):
                    stats.files_saved += 1

        elif config.crawl_type == "videos":
            candidates = extract_video_urls(soup, current_url)
            for file_url in candidates:
                if file_url in seen_download_urls:
                    continue
                seen_download_urls.add(file_url)
                if not robots.can_fetch(file_url):
                    LOGGER.debug("Video blocked by robots.txt: %s", file_url)
                    stats.urls_skipped += 1
                    continue
                if save_binary_file(session, file_url, dirs["videos"], config.timeout, seen_content_hashes, stats.files_saved + 1):
                    stats.files_saved += 1

        elif config.crawl_type == "text":
            title, text = extract_text_content(soup)
            slug = safe_filename(Path(urlparse(current_url).path).stem or "index")
            text_file = dirs["text"] / f"{slug}_{stats.pages_visited}.txt"
            try:
                text_file.write_text(text, encoding="utf-8")
            except OSError as exc:
                LOGGER.warning("Failed writing text file %s: %s", text_file, exc)
            else:
                stats.files_saved += 1
                append_jsonl(
                    jsonl_path,
                    {
                        "url": current_url,
                        "title": title,
                        "extracted_text": text,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                LOGGER.info("Saved text: %s", text_file)

        if config.depth > current_depth:
            for link in extract_links(soup, current_url):
                if is_same_domain(config.start_url, link) and link not in visited:
                    queue.append((link, current_depth + 1))

        time.sleep(config.delay)

    return stats


def parse_headers(raw_headers: str | None) -> dict[str, str]:
    if not raw_headers:
        return {}
    try:
        parsed = json.loads(raw_headers)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid --headers JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()):
        raise ValueError("--headers must be a JSON object of string keys and string values")
    return parsed


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polite crawler/downloader for images, videos, or text.")
    parser.add_argument("--url", required=True, help="Starting URL")
    parser.add_argument("--type", required=True, choices=["images", "text", "videos"], help="Content type to download")
    parser.add_argument("--out", default="downloads", help="Output directory")
    parser.add_argument("--depth", type=int, default=0, help="Same-domain crawl depth")
    parser.add_argument("--max-pages", type=int, default=30, help="Maximum HTML pages to visit")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout in seconds")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between requests in seconds")
    parser.add_argument("--headers", default=None, help="Optional JSON string of request headers")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug logging")
    return parser.parse_args(argv)


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("--url must be a valid http(s) URL")
    return url


def setup_logging(log_dir: Path, verbose: bool) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    log_path = log_dir / "crawler.log"

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    try:
        start_url = validate_url(args.url)
        headers = parse_headers(args.headers)
    except ValueError as exc:
        print(f"Argument error: {exc}", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    user_agent = headers.get(
        "User-Agent",
        "PoliteCrawler/1.0 (+https://example.com/contact; respects robots.txt and rate limits)",
    )
    headers["User-Agent"] = user_agent

    setup_logging(out_dir / "logs", args.verbose)

    LOGGER.info("Reminder: Ensure you comply with website Terms of Service and authorization requirements.")
    config = CrawlConfig(
        start_url=start_url,
        crawl_type=args.type,
        out_dir=out_dir,
        depth=max(0, args.depth),
        max_pages=max(1, args.max_pages),
        timeout=max(1, args.timeout),
        delay=max(0.0, args.delay),
        headers=headers,
        user_agent=user_agent,
    )

    start = time.time()
    stats = crawl(config)
    elapsed = time.time() - start

    LOGGER.info("=== Crawl Summary ===")
    LOGGER.info("Type: %s", config.crawl_type)
    LOGGER.info("Pages visited: %s", stats.pages_visited)
    LOGGER.info("Files saved: %s", stats.files_saved)
    LOGGER.info("URLs skipped: %s", stats.urls_skipped)
    LOGGER.info("Output directory: %s", config.out_dir.resolve())
    LOGGER.info("Elapsed: %.2fs", elapsed)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
