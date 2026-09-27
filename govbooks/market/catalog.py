"""A no-key fallback: counts existing ISBN editions of a book in Google Books and Open Library.

It cannot see Amazon sales ranks. A print book's ISBN-10 is also its Amazon ASIN, so each
edition found here links straight to its Amazon page for a manual look.
"""

from __future__ import annotations

from govbooks.http import Http
from govbooks.models import Listing
from govbooks.sources import quote
from govbooks.sources.google_books import GoogleBooksSource, isbns_of
from govbooks.text import as_list, isbn13_to_isbn10

OPEN_LIBRARY_SEARCH = "https://openlibrary.org/search.json"


class CatalogProvider:
    name = "catalog"
    has_sales_rank = False

    def __init__(self, http: Http, google_api_key: str | None = None, marketplace: str = "www.amazon.com"):
        self.http = http
        self.google = GoogleBooksSource(http, google_api_key)
        self.marketplace = marketplace

    def _listing(self, isbns: list[str], title: str, authors: list[str], price: float | None = None) -> Listing | None:
        asin = next((a for a in (isbn13_to_isbn10(i) for i in isbns) if a), None)
        if not asin:
            return None
        return Listing(
            asin=asin, title=title, price=price, url=f"https://{self.marketplace}/dp/{asin}", authors=authors
        )

    def search(self, query: str, limit: int = 10) -> list[Listing]:
        found: dict[str, Listing] = {}
        for item in self.google.query(f"intitle:{quote(query)}", limit=20):
            info = item.get("volumeInfo") or {}
            price = ((item.get("saleInfo") or {}).get("listPrice") or {}).get("amount")
            listing = self._listing(isbns_of(info), info.get("title") or "", list(info.get("authors") or []), price)
            if listing:
                found.setdefault(listing.asin, listing)
        data = self.http.get_json(
            OPEN_LIBRARY_SEARCH, params={"title": query, "fields": "title,author_name,isbn", "limit": 20}
        )
        for doc in data.get("docs") or []:
            # One Open Library work can list many editions; count the work once.
            listing = self._listing(as_list(doc.get("isbn")), doc.get("title") or "", as_list(doc.get("author_name")))
            if listing:
                found.setdefault(listing.asin, listing)
        return list(found.values())

    def lookup(self, asins: list[str]) -> list[Listing]:
        result = []
        for asin in asins:
            items = list(self.google.query(f"isbn:{asin}", limit=1))
            if items:
                info = items[0].get("volumeInfo") or {}
                result.append(
                    Listing(asin=asin, title=info.get("title") or "", url=f"https://{self.marketplace}/dp/{asin}")
                )
        return result
