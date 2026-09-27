"""Amazon Creators API, which replaced the Product Advertising API (PA-API 5.0) in 2026.

Needs Amazon Associates credentials: AMAZON_CREATORS_CREDENTIAL_ID,
AMAZON_CREATORS_CREDENTIAL_SECRET and AMAZON_PARTNER_TAG. Older (v2) credentials use a
different token endpoint and scope; set AMAZON_CREATORS_TOKEN_URL and
AMAZON_CREATORS_SCOPE to the values shown in Associates Central.
"""

from __future__ import annotations

import time
from typing import Any

from govbooks.http import Http
from govbooks.market.base import ProviderError
from govbooks.models import Listing

API = "https://creatorsapi.amazon/catalog/v1"
DEFAULT_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
DEFAULT_SCOPE = "creatorsapi::default"
RESOURCES = [
    "itemInfo.title",
    "itemInfo.byLineInfo",
    "itemInfo.classifications",
    "offersV2.listings.price",
    "browseNodeInfo.websiteSalesRank",
]
MAX_ITEMS_PER_CALL = 10


def dig(obj: Any, *path: str) -> Any:
    """Nested lookup that ignores key case (PA-API used PascalCase, Creators API camelCase).
    Lists are entered at their first element."""
    for key in path:
        if isinstance(obj, list):
            obj = obj[0] if obj else None
        if not isinstance(obj, dict):
            return None
        wanted = key.lower()
        obj = next((v for k, v in obj.items() if k.lower() == wanted), None)
    return obj


class CreatorsProvider:
    name = "creators"
    has_sales_rank = True

    def __init__(
        self,
        http: Http,
        credential_id: str,
        credential_secret: str,
        partner_tag: str,
        marketplace: str = "www.amazon.com",
        token_url: str = DEFAULT_TOKEN_URL,
        scope: str = DEFAULT_SCOPE,
    ):
        self.http = http
        self.credential_id = credential_id
        self.credential_secret = credential_secret
        self.partner_tag = partner_tag
        self.marketplace = marketplace
        self.token_url = token_url
        self.scope = scope
        self._token: str | None = None
        self._token_expires = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires:
            return self._token
        data = self.http.post_json(
            self.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.credential_id,
                "client_secret": self.credential_secret,
                "scope": self.scope,
            },
        )
        if "access_token" not in data:
            raise ProviderError(f"Creators API token request failed: {data}")
        self._token = data["access_token"]
        self._token_expires = time.time() + int(data.get("expires_in", 3600)) - 60
        return self._token

    def _call(self, operation: str, body: dict) -> dict:
        data = self.http.post_json(
            f"{API}/{operation}",
            json={"partnerTag": self.partner_tag, "marketplace": self.marketplace, "resources": RESOURCES, **body},
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "x-marketplace": self.marketplace,
                "Content-Type": "application/json",
            },
        )
        errors = dig(data, "errors")
        if errors and not (dig(data, "searchResult") or dig(data, "itemsResult")):
            first = errors[0] if isinstance(errors, list) and errors else errors
            # Amazon answers "no results" with an error object rather than an empty list.
            if "noresults" in str(dig(first, "code") or "").lower():
                return {}
            raise ProviderError(f"Creators API {operation}: {dig(first, 'message') or first}")
        return data

    def search(self, query: str, limit: int = 10) -> list[Listing]:
        data = self._call(
            "searchItems",
            {"keywords": query, "searchIndex": "Books", "itemCount": min(MAX_ITEMS_PER_CALL, limit)},
        )
        items = dig(data, "searchResult")
        items = dig(items, "items") if items else None
        return [item for item in (parse_item(i, self.marketplace) for i in items or []) if item]

    def lookup(self, asins: list[str]) -> list[Listing]:
        result = []
        for start in range(0, len(asins), MAX_ITEMS_PER_CALL):
            chunk = asins[start : start + MAX_ITEMS_PER_CALL]
            data = self._call("getItems", {"itemIds": chunk, "itemIdType": "ASIN"})
            items = dig(data, "itemsResult")
            items = dig(items, "items") if items else None
            result.extend(item for item in (parse_item(i, self.marketplace) for i in items or []) if item)
        return result


def parse_item(item: dict, marketplace: str = "www.amazon.com") -> Listing | None:
    asin = dig(item, "asin")
    if not asin:
        return None
    contributors = dig(item, "itemInfo", "byLineInfo")
    contributors = dig(contributors, "contributors") if contributors else None
    authors = [dig(c, "name") for c in contributors or [] if isinstance(c, dict)]
    rank = dig(item, "browseNodeInfo", "websiteSalesRank", "salesRank")
    price = dig(item, "offersV2", "listings", "price", "money", "amount")
    return Listing(
        asin=asin,
        title=dig(item, "itemInfo", "title", "displayValue") or "",
        sales_rank=int(rank) if rank else None,
        price=float(price) if price is not None else None,
        url=dig(item, "detailPageURL") or f"https://{marketplace}/dp/{asin}",
        authors=[a for a in authors if a],
        binding=dig(item, "itemInfo", "classifications", "binding", "displayValue"),
    )
