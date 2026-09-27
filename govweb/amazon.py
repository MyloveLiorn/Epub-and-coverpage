"""Checking whether books found on government websites are already sold on Amazon.

Uses the govbooks market providers: "catalog" (free; finds editions with an ISBN, whose ISBN-10
is also the Amazon ASIN), "keepa" or "creators" (see Amazon directly, with sales rank).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Callable
from urllib.parse import urlencode

from govbooks.http import HttpError
from govbooks.market import MarketProvider, ProviderError, search_query
from govbooks.market.base import evaluate
from govweb import db


def check_documents(
    conn: sqlite3.Connection,
    provider: MarketProvider,
    docs: list[sqlite3.Row],
    progress: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    """Returns (checked, found on Amazon)."""
    checked = found = 0
    for doc in docs:
        query = search_query(doc["title"])
        if len(query) < 4:  # nothing meaningful to search for
            continue
        try:
            listings = provider.search(query, limit=10)
        except (HttpError, ProviderError, OSError, ValueError) as exc:
            if progress:
                progress(f"  {query!r}: failed ({exc})")
            continue
        result = evaluate(doc["title"], listings, None, "public_domain", provider.has_sales_rank)
        db.save_amazon_check(
            conn,
            {
                "url": doc["url"],
                "checked_at": db.now(),
                "provider": provider.name,
                "query": query,
                "matching": len(result.matching),
                "asins": [item.asin for item in result.matching],
                "best_rank": result.best_rank,
                "price": result.median_price,
            },
        )
        checked += 1
        found += bool(result.matching)
        if progress:
            status = f"on Amazon ({len(result.matching)} edition(s))" if result.matching else "not found"
            progress(f"  {query!r}: {status}")
    return checked, found


def amazon_status(row: sqlite3.Row) -> str:
    if row["amazon_checked_at"] is None:
        return "not checked"
    if row["amazon_matching"]:
        return "yes"
    # The free catalog check only sees editions with an ISBN; Kindle-only listings can be missed.
    return "not found" if row["amazon_provider"] == "catalog" else "no"


def amazon_link(row: sqlite3.Row, marketplace: str = "www.amazon.com") -> str:
    asins = json.loads(row["amazon_asins"]) if row["amazon_asins"] else []
    if asins:
        return f"https://{marketplace}/dp/{asins[0]}"
    return f"https://{marketplace}/s?" + urlencode({"k": search_query(row["title"]), "i": "stripbooks"})
