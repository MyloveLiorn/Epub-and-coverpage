"""Internet Archive: scanned federal and state documents with downloadable full text."""

from __future__ import annotations

import re
from typing import Iterator

from govbooks.http import Http
from govbooks.models import Record
from govbooks.sources import quote
from govbooks.text import as_list, parse_year, strip_tags

SEARCH_URL = "https://archive.org/advancedsearch.php"
FIELDS = ["identifier", "title", "creator", "publisher", "date", "year", "subject", "description", "isbn"]
GOV_PUBLISHERS = '("government printing office" OR "government publishing office")'


class InternetArchiveSource:
    name = "internet_archive"

    def __init__(self, http: Http, collections: list[str]):
        self.http = http
        self.collections = collections

    def search(self, keyword: str, author: str | None = None, limit: int = 100) -> Iterator[Record]:
        kw = quote(keyword)
        if author:
            scope = f"creator:({quote(author)})"
        else:
            parts = [f"publisher:{GOV_PUBLISHERS}"]
            if self.collections:
                parts.insert(0, f"collection:({' OR '.join(self.collections)})")
            scope = "(" + " OR ".join(parts) + ")"
        query = f"(title:({kw}) OR subject:({kw})) AND {scope} AND mediatype:texts"
        rows = min(100, limit)
        page, count = 1, 0
        while count < limit:
            data = self.http.get_json(
                SEARCH_URL,
                params={"q": query, "fl[]": FIELDS, "rows": rows, "page": page, "output": "json"},
            )
            response = data.get("response", {})
            docs = response.get("docs") or []
            for doc in docs:
                record = parse_doc(doc)
                if record:
                    count += 1
                    yield record
            if not docs or page * rows >= response.get("numFound", 0):
                break
            page += 1


def _first(value: object) -> str | None:
    items = as_list(value)
    return items[0] if items else None


def parse_doc(doc: dict) -> Record | None:
    identifier = doc.get("identifier")
    title = _first(doc.get("title"))
    if not identifier or not title:
        return None
    subjects = [s.strip() for raw in as_list(doc.get("subject")) for s in re.split(r"\s*;\s*", raw) if s.strip()]
    description = " ".join(strip_tags(d) for d in as_list(doc.get("description"))).strip() or None
    url = f"https://archive.org/details/{identifier}"
    return Record(
        source="internet_archive",
        source_id=identifier,
        title=title.strip(),
        authors=as_list(doc.get("creator")),
        publisher=_first(doc.get("publisher")),
        year=parse_year(doc.get("year")) or parse_year(doc.get("date")),
        subjects=subjects,
        description=description[:2000] if description else None,
        isbns=as_list(doc.get("isbn")),
        url=url,
        fulltext_url=url,
    )
