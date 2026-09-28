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
from govbooks.models import Listing
from govbooks.text import main_title, title_tokens
from govweb import db

# Words that don't tell one book from another: a title made only of these ("2021 Annual Report",
# "Download") matches unrelated books on Amazon.
GENERIC_TITLE_WORDS = {
    "report", "annual", "guide", "handbook", "manual", "download", "book", "document", "file", "pdf", "final",
    "draft", "version", "update", "updated", "new", "all", "about", "overview", "introduction", "summary",
    "information", "info", "resource", "fact", "sheet", "plan", "program", "page", "form", "volume", "part",
    "edition", "revised", "public", "quick", "reference", "best", "practice", "tip", "general",
}  # fmt: skip
# Words in agency names that don't identify the agency.
OWNER_NOISE = {"department", "office", "united", "state", "government", "agency", "bureau", "division",
               "administration", "commission", "board", "national", "federal", "public", "service", "council"}  # fmt: skip
SKIPPED = "skipped"  # provider name recorded for titles too generic to search


def distinctive_words(title: str) -> set[str]:
    return {w for w in title_tokens(main_title(title)) if w not in GENERIC_TITLE_WORDS and not w.isdigit()}


def _by_owner(listings: list[Listing], doc: sqlite3.Row) -> list[Listing]:
    """Listings whose author or title names the document's publisher (for short titles, a
    same-title book by someone else is likelier than a reprint)."""
    owner = title_tokens(f"{doc['organization'] or ''} {doc['suborganization'] or ''}") - OWNER_NOISE
    return [item for item in listings if owner & title_tokens(f"{' '.join(item.authors)} {item.title}")]


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
        words = distinctive_words(doc["title"])
        if len(words) < 2:  # "Report", "2021 Annual Report": any match would be a coincidence
            db.save_amazon_check(conn, {"url": doc["url"], "checked_at": db.now(), "provider": SKIPPED, "query": "",
                                        "matching": 0, "asins": [], "best_rank": None, "price": None})  # fmt: skip
            continue
        try:
            listings = provider.search(query, limit=10)
        except (HttpError, ProviderError, OSError, ValueError) as exc:
            if progress:
                progress(f"  {query!r}: failed ({exc})")
            continue
        if len(words) < 4:
            listings = _by_owner(listings, doc)
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
    if row["amazon_provider"] == SKIPPED:
        return "title too generic to check"
    if row["amazon_matching"]:
        return "yes"
    # The free catalog check only sees editions with an ISBN; Kindle-only listings can be missed.
    return "not found" if row["amazon_provider"] == "catalog" else "no"


def amazon_link(row: sqlite3.Row, marketplace: str = "www.amazon.com") -> str:
    asins = json.loads(row["amazon_asins"]) if row["amazon_asins"] else []
    if asins:
        return f"https://{marketplace}/dp/{asins[0]}"
    return f"https://{marketplace}/s?" + urlencode({"k": search_query(row["title"]), "i": "stripbooks"})
