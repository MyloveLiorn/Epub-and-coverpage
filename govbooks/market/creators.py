"""Amazon Creators API, which replaced the Product Advertising API (PA-API 5.0) in 2026.

Needs Amazon Associates credentials (version 3.x, "Login with Amazon"):
AMAZON_CREATORS_CREDENTIAL_ID, AMAZON_CREATORS_CREDENTIAL_SECRET and AMAZON_PARTNER_TAG.
The older 2.x (Cognito) credentials stopped working on 11 September 2026. The token
endpoint follows the marketplace's region; AMAZON_CREATORS_TOKEN_URL and
AMAZON_CREATORS_SCOPE override it if Associates Central shows different values.
"""

from __future__ import annotations

import time
from typing import Any

from govbooks.http import Http
from govbooks.market.base import ProviderError
from govbooks.models import Listing

API = "https://creatorsapi.amazon/catalog/v1"
# Login with Amazon token endpoints by region.
TOKEN_URLS = {
    "NA": "https://api.amazon.com/auth/o2/token",
    "EU": "https://api.amazon.co.uk/auth/o2/token",
    "FE": "https://api.amazon.co.jp/auth/o2/token",
}
NA_MARKETPLACES = {"www.amazon.com", "www.amazon.ca", "www.amazon.com.mx", "www.amazon.com.br"}
FE_MARKETPLACES = {"www.amazon.co.jp", "www.amazon.com.au", "www.amazon.sg"}
DEFAULT_TOKEN_URL = TOKEN_URLS["NA"]
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


def token_url_for(marketplace: str) -> str:
    if marketplace in NA_MARKETPLACES:
        return TOKEN_URLS["NA"]
    if marketplace in FE_MARKETPLACES:
        return TOKEN_URLS["FE"]
    return TOKEN_URLS["EU"]


def _items(data: dict, wrapper: str) -> list:
    """Items sit under searchResult/itemsResult; accept them at the top level too."""
    items = dig(data, wrapper, "items") if dig(data, wrapper) else dig(data, "items")
    return items if isinstance(items, list) else []


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
            json={
                "partnerTag": self.partner_tag,
                "partnerType": "Associates",
                "marketplace": self.marketplace,
                "resources": RESOURCES,
                **body,
            },
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "x-marketplace": self.marketplace,
                "Content-Type": "application/json",
            },
        )
        errors = dig(data, "errors")
        if errors and not (dig(data, "searchResult") or dig(data, "itemsResult") or dig(data, "items")):
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
        return [item for item in (parse_item(i, self.marketplace) for i in _items(data, "searchResult")) if item]

    def lookup(self, asins: list[str]) -> list[Listing]:
        result = []
        for start in range(0, len(asins), MAX_ITEMS_PER_CALL):
            chunk = asins[start : start + MAX_ITEMS_PER_CALL]
            data = self._call("getItems", {"itemIds": chunk, "itemIdType": "ASIN"})
            result.extend(item for item in (parse_item(i, self.marketplace) for i in _items(data, "itemsResult")) if item)
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
