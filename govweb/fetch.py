"""Polite page fetching: robots.txt, per-host delays, size caps and HTML-only reads."""

from __future__ import annotations

import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

from govbooks.http import CappedRetry
from govweb import __version__

# The usual form for a crawler's user agent (as Googlebot and Bingbot use): it names the crawler
# and where to read about it. Some sites refuse user agents that don't start with "Mozilla/5.0".
USER_AGENT = f"Mozilla/5.0 (compatible; govweb/{__version__}; +https://github.com/MyloveLiorn/Epub-and-coverpage)"
ROBOTS_NAME = "govweb"  # the name robots.txt rules are matched against
HTML_TYPES = ("text/html", "application/xhtml+xml")
MAX_CRAWL_DELAY = 10.0


@dataclass
class FetchResult:
    url: str  # after redirects
    status: int | None
    content_type: str = ""
    body: bytes = b""
    error: str | None = None
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class Fetcher:
    """One per crawled site. Not thread-safe; run one Fetcher per worker thread."""

    def __init__(
        self, min_interval: float = 1.0, timeout: tuple[float, float] = (10.0, 30.0), max_bytes: int = 2_000_000
    ):
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._last_call: dict[str, float] = {}
        self._delays: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        # A site that doesn't accept connections won't within seconds either: fail fast on connect
        # errors (timeout is (connect, read)), retry only on overload responses.
        retry = CappedRetry(
            total=2, connect=0, backoff_factor=1, status_forcelist=(429, 502, 503, 504),
            respect_retry_after_header=True,
        )  # fmt: skip
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _throttle(self, host: str) -> None:
        interval = max(self.min_interval, self._delays.get(host, 0.0))
        wait = self._last_call.get(host, 0.0) + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call[host] = time.monotonic()

    def get(self, url: str, html_only: bool = False, max_bytes: int | None = None) -> FetchResult:
        """GET a URL, reading at most max_bytes. With html_only, non-HTML bodies are not read."""
        limit = max_bytes or self.max_bytes
        self._throttle(urlsplit(url).netloc)
        try:
            with self.session.get(url, timeout=self.timeout, stream=True, allow_redirects=True) as resp:
                content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                result = FetchResult(url=resp.url, status=resp.status_code, content_type=content_type)
                if html_only and content_type not in HTML_TYPES:
                    return result
                chunks, size = [], 0
                for chunk in resp.iter_content(64 * 1024):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > limit:
                        result.truncated = True
                        break
                result.body = b"".join(chunks)[:limit]
                return result
        except requests.RequestException as exc:
            return FetchResult(url=url, status=None, error=str(exc)[:300])

    def robots(self, base_url: str) -> urllib.robotparser.RobotFileParser:
        """The robots.txt rules for a scheme://host. Missing or unreadable files allow everything."""
        parts = urlsplit(base_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = urllib.robotparser.RobotFileParser(origin + "/robots.txt")
            result = self.get(origin + "/robots.txt", max_bytes=500_000)
            if result.ok and not result.content_type.startswith("text/html"):
                parser.parse(result.text().splitlines())
            elif result.status in (401, 403):
                parser.disallow_all = True
            else:
                parser.allow_all = True
            delay = parser.crawl_delay(ROBOTS_NAME)
            if delay:
                self._delays[parts.netloc] = min(float(delay), MAX_CRAWL_DELAY)
            self._robots[origin] = parser
        return self._robots[origin]

    def allowed(self, url: str) -> bool:
        return self.robots(url).can_fetch(ROBOTS_NAME, url)
