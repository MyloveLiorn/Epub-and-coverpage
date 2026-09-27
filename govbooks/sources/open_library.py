"""Open Library: a large book catalog that includes many government publications."""

from __future__ import annotations

from typing import Iterator

from govbooks.http import Http
from govbooks.models import Record
from govbooks.sources import quote
from govbooks.text import as_list, contains_phrase, normalize_name

SEARCH_URL = "https://openlibrary.org/search.json"
FIELDS = "key,title,subtitle,author_name,publisher,first_publish_year,subject,isbn,ia,ebook_access"
GOV_PUBLISHERS = ['"Government Printing Office"', '"Government Publishing Office"', '"Superintendent of Documents"']
_GOV_WORDS = ("united states", "government", "department", "office", "bureau", "agency", "commission")


class OpenLibrarySource:
    name = "open_library"

    def __init__(self, http: Http):
        self.http = http

    def search(self, keyword: str, author: str | None = None, limit: int = 100) -> Iterator[Record]:
        kw = quote(keyword)
        scope = f"author:{quote(author)}" if author else f"publisher:({' OR '.join(GOV_PUBLISHERS)})"
        query = f"(title:{kw} OR subject:{kw}) AND {scope}"
        per_page = min(100, limit)
        page, count = 1, 0
        while count < limit:
            data = self.http.get_json(
                SEARCH_URL, params={"q": query, "fields": FIELDS, "limit": per_page, "page": page}
            )
            docs = data.get("docs") or []
            for doc in docs:
                record = parse_doc(doc)
                if record:
                    count += 1
                    yield record
            if not docs or page * per_page >= data.get("numFound", 0):
                break
            page += 1


def parse_doc(doc: dict) -> Record | None:
    key, title = doc.get("key"), doc.get("title")
    if not key or not title:
        return None
    publishers = as_list(doc.get("publisher"))
    publisher = next((p for p in publishers if any(contains_phrase(normalize_name(p), w) for w in _GOV_WORDS)), None)
    ia = as_list(doc.get("ia"))
    free = doc.get("ebook_access") == "public"
    return Record(
        source="open_library",
        source_id=key,
        title=title.strip(),
        subtitle=doc.get("subtitle"),
        authors=as_list(doc.get("author_name")),
        publisher=publisher or (publishers[0] if publishers else None),
        year=doc.get("first_publish_year"),
        subjects=as_list(doc.get("subject"))[:30],
        isbns=as_list(doc.get("isbn"))[:10],
        url=f"https://openlibrary.org{key}",
        fulltext_url=f"https://archive.org/details/{ia[0]}" if free and ia else None,
        public_domain=True if free else None,
    )
