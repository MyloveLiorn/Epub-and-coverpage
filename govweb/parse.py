"""Reading HTML pages (title and links) and XML sitemaps."""

from __future__ import annotations

import gzip
import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

MAX_SITEMAP_BYTES = 20_000_000


@dataclass
class Link:
    url: str
    text: str


@dataclass
class Page:
    title: str = ""
    links: list[Link] = field(default_factory=list)
    nofollow: bool = False


class _PageParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.page = Page()
        self._in_title = False
        self._href: str | None = None
        self._text: list[str] = []
        self._title_attr = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "base" and attrs.get("href"):
            self.base_url = urljoin(self.base_url, attrs["href"])
        elif tag == "title":
            self._in_title = True
        elif tag == "meta" and (attrs.get("name") or "").lower() == "robots":
            if "nofollow" in (attrs.get("content") or "").lower():
                self.page.nofollow = True
        elif tag == "a" and attrs.get("href"):
            self._close_link()
            self._href = attrs["href"]
            self._text = []
            self._title_attr = " ".join((attrs.get("title") or "").split())
        elif tag == "img" and self._href is not None and attrs.get("alt"):
            self._text.append(attrs["alt"])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "a":
            self._close_link()

    def handle_data(self, data):
        if self._in_title:
            self.page.title += data
        if self._href is not None:
            self._text.append(data)

    def _close_link(self):
        if self._href is None:
            return
        href, self._href = self._href.strip(), None
        if href.lower().startswith(("mailto:", "javascript:", "tel:", "data:", "#")):
            return
        url = normalize_url(urljoin(self.base_url, href))
        if url:
            self.page.links.append(Link(url, _link_text(" ".join(" ".join(self._text).split()), self._title_attr)))
        self._text = []

    def close(self):
        super().close()
        self._close_link()
        self.page.title = " ".join(self.page.title.split())


def _link_text(body: str, title_attr: str) -> str:
    """The link's text and its title attribute, without saying the same thing twice."""
    def within(part: str, whole: str) -> bool:  # as whole words
        return f" {part.lower()} " in f" {whole.lower()} "

    if not title_attr or within(title_attr, body):
        return body
    if not body or within(body, title_attr):
        return title_attr
    return f"{title_attr} {body}"


def parse_html(html: str, base_url: str) -> Page:
    parser = _PageParser(base_url)
    parser.feed(html)
    parser.close()
    return parser.page


def normalize_url(url: str) -> str | None:
    """Drop fragments, keep http(s) only, lowercase the host."""
    url, _ = urldefrag(url.strip())
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return parts._replace(netloc=parts.netloc.lower(), path=parts.path or "/").geturl()


def _decompress(body: bytes) -> bytes:
    if body[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as fh:
            return fh.read(MAX_SITEMAP_BYTES)  # bounded, in case of a decompression bomb
    return body


def parse_sitemap(body: bytes) -> tuple[list[str], list[str]]:
    """Returns (page URLs, child sitemap URLs) from a urlset or sitemapindex document."""
    data = _decompress(body)
    head = data[:2000].lower()
    if b"<!doctype" in head or b"<!entity" in head:  # sitemaps never need entities
        return [], []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return [], []
    tag = re.sub(r"^\{.*\}", "", root.tag)
    locs = [
        (el.text or "").strip()
        for el in root.iter()
        if re.sub(r"^\{.*\}", "", el.tag) == "loc" and (el.text or "").strip()
    ]
    if tag == "sitemapindex":
        return [], locs
    return locs, []
