"""GovInfo, the Government Publishing Office's library of federal publications.

Needs an api.data.gov key in GOVINFO_API_KEY (DEMO_KEY works, with low rate limits).
Default collections: GPO (Additional Government Publications), CPRT (Congressional
Committee Prints), GAOREPORTS and ERP (Economic Report of the President).
"""

from __future__ import annotations

from typing import Iterator

from govbooks.http import Http
from govbooks.models import Record
from govbooks.sources import quote
from govbooks.text import as_list, parse_year

SEARCH_URL = "https://api.govinfo.gov/search"


class GovInfoSource:
    name = "govinfo"

    def __init__(self, http: Http, api_key: str, collections: list[str]):
        self.http = http
        self.api_key = api_key
        self.collections = collections

    def search(self, keyword: str, author: str | None = None, limit: int = 100) -> Iterator[Record]:
        if author:
            # GovInfo is federal-only and has no reliable author field; agency-by-agency
            # searches are left to the catalog sources.
            return
        query = f"title:({quote(keyword)})"
        if self.collections:
            query += f" AND collection:({' OR '.join(self.collections)})"
        offset, count = "*", 0
        while count < limit:
            data = self.http.post_json(
                SEARCH_URL,
                params={"api_key": self.api_key},
                json={
                    "query": query,
                    "pageSize": min(100, limit - count),
                    "offsetMark": offset,
                    "sorts": [{"field": "score", "sortOrder": "DESC"}],
                },
            )
            results = data.get("results") or []
            for item in results:
                record = parse_result(item)
                if record:
                    count += 1
                    yield record
            next_offset = data.get("offsetMark")
            if not results or not next_offset or next_offset == offset:
                break
            offset = next_offset


def parse_result(item: dict) -> Record | None:
    package = item.get("packageId")
    if not package or not item.get("title"):
        return None
    granule = item.get("granuleId")
    url = f"https://www.govinfo.gov/app/details/{package}" + (f"/{granule}" if granule else "")
    authors = as_list(item.get("governmentAuthor")) or [
        a for a in (item.get("governmentAuthor1"), item.get("governmentAuthor2")) if a
    ]
    return Record(
        source="govinfo",
        source_id=f"{package}/{granule}" if granule else package,
        title=item["title"].strip(),
        authors=authors,
        publisher="U.S. Government Publishing Office",
        year=parse_year(item.get("dateIssued")),
        url=url,
        fulltext_url=url,
    )
