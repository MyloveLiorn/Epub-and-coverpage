"""Keepa (keepa.com): Amazon sales rank and price data, built for tracking. Needs KEEPA_API_KEY."""

from __future__ import annotations

from govbooks.http import Http
from govbooks.market.base import ProviderError
from govbooks.models import Listing

API = "https://api.keepa.com"
DOMAINS = {
    "www.amazon.com": 1,
    "www.amazon.co.uk": 2,
    "www.amazon.de": 3,
    "www.amazon.fr": 4,
    "www.amazon.co.jp": 5,
    "www.amazon.ca": 6,
    "www.amazon.it": 8,
    "www.amazon.es": 9,
    "www.amazon.in": 10,
    "www.amazon.com.mx": 11,
}
# Indexes into Keepa's csv/stats arrays.
AMAZON_PRICE, NEW_PRICE, SALES_RANK = 0, 1, 3
STATS_DAYS = 90


class KeepaProvider:
    name = "keepa"
    has_sales_rank = True

    def __init__(self, http: Http, api_key: str, marketplace: str = "www.amazon.com"):
        if marketplace not in DOMAINS:
            raise ProviderError(f"Keepa does not support marketplace {marketplace!r}")
        self.http = http
        self.api_key = api_key
        self.marketplace = marketplace
        self.domain = DOMAINS[marketplace]

    def _get(self, path: str, **params) -> dict:
        data = self.http.get_json(f"{API}/{path}", params={"key": self.api_key, "domain": self.domain, **params})
        if data.get("error"):
            raise ProviderError(f"Keepa: {data['error'].get('message') or data['error']}")
        return data

    def search(self, query: str, limit: int = 10) -> list[Listing]:
        data = self._get("search", type="product", term=query, stats=STATS_DAYS, page=0)
        listings = [parse_product(p, self.marketplace) for p in data.get("products") or [] if is_book(p)]
        return [item for item in listings if item][:limit]

    def lookup(self, asins: list[str]) -> list[Listing]:
        result = []
        for start in range(0, len(asins), 100):
            data = self._get("product", asin=",".join(asins[start : start + 100]), stats=STATS_DAYS)
            result.extend(item for item in (parse_product(p, self.marketplace) for p in data.get("products") or []) if item)
        return result


def is_book(product: dict) -> bool:
    group = product.get("productGroup")
    return group is None or "book" in str(group).lower()


def _current(stats: dict, index: int) -> int | None:
    values = stats.get("current") or []
    value = values[index] if len(values) > index else None
    return value if isinstance(value, int) and value > 0 else None


def parse_product(product: dict, marketplace: str = "www.amazon.com") -> Listing | None:
    asin = product.get("asin")
    if not asin:
        return None
    stats = product.get("stats") or {}
    prices = [p for p in (_current(stats, AMAZON_PRICE), _current(stats, NEW_PRICE)) if p]
    author = product.get("author")
    return Listing(
        asin=asin,
        title=product.get("title") or "",
        sales_rank=_current(stats, SALES_RANK),
        price=min(prices) / 100 if prices else None,  # Keepa prices are in cents
        url=f"https://{marketplace}/dp/{asin}",
        authors=[author] if author else [],
        binding=product.get("binding"),
    )
