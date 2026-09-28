"""Crawling one website for publication pages and documents.

The crawl reads the site's sitemaps first, then follows links from the home page. Pages
that look like publication listings, or that mention one of the searched topics, are fetched
first, so a capped crawl spends its page budget where the wanted books are likely to be.
"""

from __future__ import annotations

import heapq
import itertools
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Protocol
from urllib.parse import parse_qsl, urlsplit

from govweb.classify import (
    best_title,
    document_type,
    hint_score,
    same_site,
    title_from_url,
    topic_phrases,
    topic_score,
)
from govweb.exclude import is_excluded
from govweb.fetch import HTML_TYPES, FetchResult
from govweb.parse import MAX_SITEMAP_BYTES, parse_html, parse_sitemap

# Links to these are never pages worth fetching.
SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".css", ".js", ".json", ".xml", ".rss",
    ".mp3", ".mp4", ".mov", ".avi", ".wav", ".zip", ".gz", ".xls", ".xlsx", ".csv", ".ppt", ".pptx",
    ".exe", ".dmg", ".kml", ".kmz", ".ics",
)  # fmt: skip
DOCUMENT_CONTENT_TYPES = {"application/pdf": "pdf", "application/epub+zip": "epub"}
# A link that names a searched topic outranks one that only looks like a publication list.
TOPIC_WEIGHT = 3
# Pages of one shape (Details.aspx?SERIES_ID=1, =5, ...; /article/123, /article/456) are
# given this many tries; if none of them links to a document, the rest are skipped.
PATTERN_TRIES = 5


class FetcherLike(Protocol):
    def get(self, url: str, html_only: bool = False, max_bytes: int | None = None) -> FetchResult: ...

    def robots(self, base_url: str): ...

    def allowed(self, url: str) -> bool: ...


@dataclass
class CrawlLimits:
    max_pages: int = 100
    max_depth: int = 3
    max_sitemap_files: int = 10
    use_sitemaps: bool = True
    # Words or phrases of the searched topics ("beekeeping", "first aid"): links mentioning
    # them are followed first.
    topic_keywords: list[str] = field(default_factory=list)
    # Stop a site after this long, so one slow site (a long robots.txt crawl delay) can't
    # hold up the others. None: no limit.
    max_seconds: float | None = None


@dataclass
class FoundPage:
    url: str
    title: str
    depth: int
    status: int | None
    hint_score: int
    found_via: str


@dataclass
class FoundDocument:
    url: str
    title: str
    file_type: str
    found_on: str
    found_via: str


@dataclass
class SiteResult:
    domain: str  # the host that was crawled, e.g. water.ca.gov
    status: str = "ok"  # ok, unreachable, blocked_by_robots, alias (redirects to another mapped site)
    home_url: str | None = None
    error: str | None = None
    sitemap_urls: int = 0
    pages: list[FoundPage] = field(default_factory=list)
    documents: dict[str, FoundDocument] = field(default_factory=dict)
    # Other hosts of the same registered domain that pages linked to, with link counts.
    other_hosts: Counter = field(default_factory=Counter)


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().split(":")[0]


def page_pattern(url: str) -> str:
    """ "/PubForm/Series_Details.aspx?SERIES_ID=15" -> "/PubForm/Series_Details.aspx?SERIES_ID",
    "/article/286911" -> "/article/#": pages that differ only in an id."""
    parts = urlsplit(url)
    keys = sorted({k for k, _ in parse_qsl(parts.query, keep_blank_values=True)})
    return re.sub(r"\d+", "#", parts.path.lower()) + ("?" + "&".join(keys) if keys else "")


def _bare(host: str) -> str:
    return host.removeprefix("www.")


def _find_home(host: str, fetcher: FetcherLike) -> tuple[str, FetchResult | None]:
    """Try https://host, https://www.host and http://host. On failure, return the most telling
    result: an HTTP answer beats a timeout, which beats a missing DNS name."""
    candidates = [f"https://{host}/", f"http://{host}/"]
    if not host.startswith("www."):
        candidates.insert(1, f"https://www.{host}/")
    tried: list[FetchResult] = []
    timed_out: set[str] = set()
    for url in candidates:
        if _host(url) in timed_out:  # the same server won't answer on another port either
            continue
        result = fetcher.get(url, html_only=True)
        if result.ok:
            return url, result
        tried.append(result)
        if result.error and "timed out" in result.error:
            timed_out.add(_host(url))

    def telling(result: FetchResult) -> int:
        if result.status is not None:
            return 2
        return 1 if result.error and "timed out" in result.error else 0

    return "", max(tried, key=telling) if tried else None


class _Crawl:
    """Pages are fetched from one host (plus its www. twin); documents are recorded anywhere
    under the registered domain, and other subdomains are noted for their own crawl."""

    def __init__(
        self, host: str, registered_domain: str, fetcher: FetcherLike, limits: CrawlLimits, seeds: list[str],
        is_mapped: Callable[[str], bool] | None = None,
    ):  # fmt: skip
        self.seeds = seeds
        self.is_mapped = is_mapped
        self.fetcher = fetcher
        self.limits = limits
        self.result = SiteResult(host)
        self.domains = [registered_domain]  # documents are kept from anywhere under these
        self.hosts = {_bare(host)}  # pages are fetched only from these
        self.queue: list[tuple[int, int, int, int, str, str]] = []
        self.seen: set[str] = set()
        self.fetches = 0
        self.counter = itertools.count()
        self.patterns: dict[str, list[int]] = {}  # page shape -> [pages fetched, documents they linked to]
        # Link text and linking page's title of queued pages, for those that turn out to be documents.
        self.anchors: dict[str, tuple[str, str]] = {}
        self.phrases = topic_phrases(limits.topic_keywords)

    def priority(self, url: str, text: str) -> int:
        return hint_score(url, text) + TOPIC_WEIGHT * topic_score(url, text, self.phrases)

    def add_document(self, url: str, title: str, kind: str, found_on: str, via: str) -> None:
        existing = self.result.documents.get(url)
        # Keep the most descriptive title seen for a document.
        if existing is None or (existing.title == title_from_url(url) and title != existing.title):
            self.result.documents[url] = FoundDocument(url, title, kind, found_on, via)

    def add_link(self, url: str, text: str, depth: int, found_on: str, via: str, context: str = "") -> None:
        if not any(same_site(url, d) for d in self.domains) or is_excluded(url):
            return
        kind = document_type(url)
        if kind:
            self.add_document(url, best_title(text, url, context), kind, found_on, via)
            return
        host = _bare(_host(url))
        if host not in self.hosts:
            self.result.other_hosts[host] += 1
            return
        path = urlsplit(url).path.lower()
        if url in self.seen or depth > self.limits.max_depth or path.endswith(SKIP_EXTENSIONS):
            return
        self.seen.add(url)
        self.anchors[url] = (text, context)
        score = self.priority(url, text)
        # Most promising first; query-string pages (calendars, searches) last among equals.
        penalty = 1 if urlsplit(url).query else 0
        heapq.heappush(self.queue, (-score, depth + penalty, next(self.counter), depth, url, via))

    def read_sitemaps(self, home_url: str) -> None:
        robots = self.fetcher.robots(home_url)
        origin = "{0.scheme}://{0.netloc}".format(urlsplit(home_url))
        pending = list(robots.site_maps() or []) or [origin + "/sitemap.xml"]
        done = 0
        while pending and done < self.limits.max_sitemap_files:
            sitemap_url = pending.pop(0)
            if not self.fetcher.allowed(sitemap_url):
                continue
            done += 1
            fetched = self.fetcher.get(sitemap_url, max_bytes=MAX_SITEMAP_BYTES)
            if not fetched.ok:
                continue
            urls, children = parse_sitemap(fetched.body)
            pending.extend(children)
            self.result.sitemap_urls += len(urls)
            for url in urls:
                # Documents, publication-looking pages and pages on a searched topic only;
                # ordinary pages come via links.
                if document_type(url) or self.priority(url, "") > 0:
                    self.add_link(url, "", 1, sitemap_url, "sitemap")

    def fruitless(self, url: str) -> bool:
        fetched, documents = self.patterns.get(page_pattern(url), (0, 0))
        return fetched >= PATTERN_TRIES and documents == 0

    def visit(self, url: str, depth: int, via: str, fetched: FetchResult | None = None) -> None:
        self.fetches += 1
        stats = self.patterns.setdefault(page_pattern(url), [0, 0])
        stats[0] += 1
        documents_before = len(self.result.documents)
        fetched = fetched or self.fetcher.get(url, html_only=True)
        kind = DOCUMENT_CONTENT_TYPES.get(fetched.content_type)
        if kind:  # a document served from a URL without a file extension
            text, context = self.anchors.get(url, ("", ""))
            self.add_document(url, best_title(text, url, context), kind, url, via)
            stats[1] += 1
            return
        page = None
        if fetched.ok and fetched.content_type in HTML_TYPES:
            page = parse_html(fetched.text(), fetched.url)
        self.result.pages.append(
            FoundPage(url, page.title if page else "", depth, fetched.status, hint_score(url), via)
        )
        if page and not page.nofollow:
            for link in page.links:
                self.add_link(link.url, link.text, depth + 1, url, "link", page.title)
        stats[1] += len(self.result.documents) - documents_before

    def run(self) -> SiteResult:
        deadline = time.monotonic() + self.limits.max_seconds if self.limits.max_seconds else None
        home_url, home = _find_home(self.result.domain, self.fetcher)
        if not home_url:
            self.result.status = "unreachable"
            self.result.error = (home.error if home else None) or f"HTTP {home.status if home else '?'}"
            return self.result
        final_host = _bare(_host(home.url))
        if final_host not in self.hosts and self.is_mapped and self.is_mapped(final_host):
            # presidiotrust.gov -> presidio.gov: the same website, crawled as a site of its own.
            self.result.status = "alias"
            self.result.home_url = home.url
            return self.result
        if final_host not in self.hosts:  # the site redirects elsewhere; follow it there
            self.hosts.add(final_host)
            if not any(same_site(home.url, d) for d in self.domains):
                self.domains.append(final_host)
        self.result.home_url = home.url
        if not self.fetcher.allowed(home.url):
            self.result.status = "blocked_by_robots"
            return self.result
        self.seen.add(home.url)
        if self.limits.use_sitemaps:
            self.read_sitemaps(home.url)
        self.visit(home.url, 0, "home", home)
        for url in self.seeds:  # pages that had documents or looked like listings last time
            if url not in self.seen and _bare(_host(url)) in self.hosts:
                self.seen.add(url)
                heapq.heappush(self.queue, (-100, 1, next(self.counter), 1, url, "seed"))
        while self.queue and self.fetches < self.limits.max_pages:
            if deadline and time.monotonic() > deadline:
                self.result.error = f"stopped after {self.limits.max_seconds / 60:g} minutes"
                break
            *_, depth, url, via = heapq.heappop(self.queue)
            if not self.fruitless(url) and self.fetcher.allowed(url):
                self.visit(url, depth, via)
        return self.result


def crawl_site(
    host: str,
    fetcher: FetcherLike,
    limits: CrawlLimits | None = None,
    registered_domain: str | None = None,
    seeds: list[str] | None = None,
    is_mapped: Callable[[str], bool] | None = None,
) -> SiteResult:
    """Crawl one site. ``host`` is a registry domain (usda.gov) or one of its subdomains
    (water.ca.gov, with registered_domain="ca.gov"). ``seeds`` are pages fetched right after
    the home page; re-crawls pass the pages where documents were found before, so new
    publications on known listing pages are caught first. ``is_mapped(host)`` tells whether
    a host is a site of its own on the map: a site whose home page redirects to one is an
    alias and isn't crawled twice."""
    return _Crawl(
        host, registered_domain or _bare(host), fetcher, limits or CrawlLimits(), seeds or [], is_mapped
    ).run()
