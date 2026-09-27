"""Checking whether discovered books are worth selling on Amazon."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Callable

from govbooks import db
from govbooks.config import Config, env
from govbooks.http import Http, HttpError
from govbooks.market.base import (
    VERDICT_HELP,
    MarketProvider,
    MarketResult,
    ProviderError,
    evaluate,
    topic_demand,
)
from govbooks.text import main_title

__all__ = [
    "VERDICT_HELP",
    "MarketProvider",
    "MarketResult",
    "ProviderError",
    "build_provider",
    "check_books",
    "evaluate",
    "search_query",
    "topic_demand",
]

PROVIDERS = ("catalog", "keepa", "creators")


def build_provider(name: str, http: Http, config: Config) -> MarketProvider:
    if name == "catalog":
        from govbooks.market.catalog import CatalogProvider

        return CatalogProvider(http, env("GOOGLE_BOOKS_API_KEY"), config.marketplace)
    if name == "keepa":
        from govbooks.market.keepa import KeepaProvider

        key = env("KEEPA_API_KEY")
        if not key:
            raise SystemExit("The keepa provider needs KEEPA_API_KEY in the environment.")
        return KeepaProvider(http, key, config.marketplace)
    if name == "creators":
        from govbooks.market.creators import DEFAULT_SCOPE, CreatorsProvider, token_url_for

        needed = ["AMAZON_CREATORS_CREDENTIAL_ID", "AMAZON_CREATORS_CREDENTIAL_SECRET", "AMAZON_PARTNER_TAG"]
        missing = [n for n in needed if not env(n)]
        if missing:
            raise SystemExit(f"The creators provider needs {', '.join(missing)} in the environment.")
        return CreatorsProvider(
            http,
            env("AMAZON_CREATORS_CREDENTIAL_ID"),
            env("AMAZON_CREATORS_CREDENTIAL_SECRET"),
            env("AMAZON_PARTNER_TAG"),
            config.marketplace,
            env("AMAZON_CREATORS_TOKEN_URL", token_url_for(config.marketplace)),
            env("AMAZON_CREATORS_SCOPE", DEFAULT_SCOPE),
        )
    raise SystemExit(f"Unknown Amazon provider {name!r}. Choose one of: {', '.join(PROVIDERS)}")


def search_query(title: str) -> str:
    """Search Amazon by the main title, trimmed so long catalog titles don't over-constrain it."""
    return " ".join(main_title(title).split()[:12])


def _recently_checked(conn: sqlite3.Connection, book_id: str, provider: str, days: int) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT 1 FROM market_checks WHERE book_id = ? AND provider = ? AND checked_at >= ?",
        (book_id, provider, cutoff),
    ).fetchone()
    return row is not None


def check_books(
    conn: sqlite3.Connection,
    provider: MarketProvider,
    books: list[sqlite3.Row],
    topic_rank: int | None = None,
    skip_checked_within_days: int = 7,
    progress: Callable[[str], None] | None = None,
) -> list[tuple[sqlite3.Row, MarketResult]]:
    results = []
    for book in books:
        if skip_checked_within_days and _recently_checked(conn, book["id"], provider.name, skip_checked_within_days):
            continue
        query = search_query(book["title"])
        try:
            listings = provider.search(query, limit=10)
        except (HttpError, ProviderError, OSError, ValueError) as exc:
            if progress:
                progress(f"  {query!r}: failed ({exc})")
            continue
        result = evaluate(book["title"], listings, topic_rank, book["rights"], provider.has_sales_rank)
        db.add_market_check(
            conn,
            {
                "book_id": book["id"],
                "provider": provider.name,
                "checked_at": db.now(),
                "query": query,
                "matching_listings": len(result.matching),
                "best_rank": result.best_rank,
                "median_price": result.median_price,
                "topic_rank": topic_rank,
                "verdict": result.verdict,
                "score": result.score,
                "listings": [asdict(item) for item in result.matching],
            },
        )
        if progress:
            progress(f"  {query!r}: {result.verdict}, {len(result.matching)} matching listing(s), score {result.score}")
        results.append((book, result))
    return results
