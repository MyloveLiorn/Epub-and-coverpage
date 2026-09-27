"""Google Books. Works without a key but is heavily rate limited; set GOOGLE_BOOKS_API_KEY."""

from __future__ import annotations

from typing import Iterator

from govbooks.http import Http
from govbooks.models import Record
from govbooks.sources import quote
from govbooks.text import parse_year

SEARCH_URL = "https://www.googleapis.com/books/v1/volumes"
GOV_PUBLISHERS = ["Government Printing Office", "Government Publishing Office"]


class GoogleBooksSource:
    name = "google_books"

    def __init__(self, http: Http, api_key: str | None = None):
        self.http = http
        self.api_key = api_key

    def query(self, q: str, limit: int = 40) -> Iterator[dict]:
        start = 0
        while start < limit:
            size = min(40, limit - start)
            params = {"q": q, "startIndex": start, "maxResults": size, "printType": "books"}
            if self.api_key:
                params["key"] = self.api_key
            items = self.http.get_json(SEARCH_URL, params=params).get("items") or []
            yield from items
            if len(items) < size:
                break
            start += len(items)

    def search(self, keyword: str, author: str | None = None, limit: int = 100) -> Iterator[Record]:
        scopes = [f"inauthor:{quote(author)}"] if author else [f"inpublisher:{quote(p)}" for p in GOV_PUBLISHERS]
        for scope in scopes:
            for item in self.query(f"{quote(keyword)} {scope}", limit):
                record = parse_volume(item)
                if record:
                    yield record


def isbns_of(info: dict) -> list[str]:
    return [
        i["identifier"]
        for i in info.get("industryIdentifiers") or []
        if i.get("type") in ("ISBN_10", "ISBN_13") and i.get("identifier")
    ]


def parse_volume(item: dict) -> Record | None:
    info = item.get("volumeInfo") or {}
    access = item.get("accessInfo") or {}
    if not item.get("id") or not info.get("title"):
        return None
    readable = access.get("viewability") == "ALL_PAGES" or access.get("publicDomain")
    return Record(
        source="google_books",
        source_id=item["id"],
        title=info["title"].strip(),
        subtitle=info.get("subtitle"),
        authors=list(info.get("authors") or []),
        publisher=info.get("publisher"),
        year=parse_year(info.get("publishedDate")),
        subjects=list(info.get("categories") or []),
        description=info.get("description"),
        isbns=isbns_of(info),
        url=info.get("infoLink") or info.get("canonicalVolumeLink"),
        fulltext_url=(access.get("webReaderLink") or info.get("previewLink")) if readable else None,
        public_domain=access.get("publicDomain"),
    )
