"""Watching books and Amazon listings over time.

A watch on an ASIN records its sales rank and price on every run. A watch on a book
searches Amazon for it on every run and reports when new competing editions appear.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass

from govbooks import db
from govbooks.http import HttpError
from govbooks.market import MarketProvider, ProviderError, search_query
from govbooks.models import Listing
from govbooks.text import title_match

# Report a sales rank move only when it is at least this large, relative to the old rank.
RANK_CHANGE_THRESHOLD = 0.2


@dataclass
class Change:
    watch_id: int
    label: str
    message: str


def _snapshot(watch: sqlite3.Row, provider: MarketProvider, listings: list[Listing], stamp: str) -> dict:
    ranks = sorted(item.sales_rank for item in listings if item.sales_rank)
    prices = [item.price for item in listings if item.price]
    return {
        "watch_id": watch["id"],
        "taken_at": stamp,
        "provider": provider.name,
        "title": listings[0].title if listings else None,
        "sales_rank": ranks[0] if ranks else None,
        "price": round(statistics.median(prices), 2) if prices else None,
        "listing_count": len(listings),
        "asins": sorted(item.asin for item in listings),
    }


def describe_changes(previous: sqlite3.Row | None, snap: dict) -> list[str]:
    if previous is None:
        parts = [f"{snap['listing_count']} listing(s)"]
        if snap["sales_rank"]:
            parts.append(f"best rank {snap['sales_rank']:,}")
        if snap["price"]:
            parts.append(f"price ${snap['price']:.2f}")
        return ["first snapshot: " + ", ".join(parts)]
    messages = []
    before, after = set(db.loads(previous["asins"])), set(snap["asins"])
    if after - before:
        messages.append(f"new listing(s): {', '.join(sorted(after - before))}")
    if before - after:
        messages.append(f"listing(s) gone: {', '.join(sorted(before - after))}")
    old_rank, new_rank = previous["sales_rank"], snap["sales_rank"]
    if old_rank and new_rank and abs(new_rank - old_rank) / old_rank >= RANK_CHANGE_THRESHOLD:
        direction = "improved" if new_rank < old_rank else "dropped"
        messages.append(f"sales rank {direction}: {old_rank:,} -> {new_rank:,}")
    old_price, new_price = previous["price"], snap["price"]
    if old_price and new_price and abs(new_price - old_price) >= 0.01:
        messages.append(f"price: ${old_price:.2f} -> ${new_price:.2f}")
    return messages


def run_tracking(conn: sqlite3.Connection, provider: MarketProvider) -> list[Change]:
    watches = db.list_watches(conn)
    stamp = db.now()
    changes: list[Change] = []

    def record(watch: sqlite3.Row, listings: list[Listing]) -> None:
        history = db.snapshots_for(conn, watch["id"])
        snap = _snapshot(watch, provider, listings, stamp)
        db.add_snapshot(conn, snap)
        for message in describe_changes(history[-1] if history else None, snap):
            changes.append(Change(watch["id"], watch["label"], message))

    def failed(watch: sqlite3.Row, exc: Exception) -> None:
        changes.append(Change(watch["id"], watch["label"], f"not checked: {exc}"))

    asin_watches = [w for w in watches if w["asin"]]
    if asin_watches:
        try:
            found = {item.asin: item for item in provider.lookup([w["asin"] for w in asin_watches])}
        except (HttpError, ProviderError, OSError, ValueError) as exc:
            for watch in asin_watches:
                failed(watch, exc)
        else:
            for watch in asin_watches:
                listing = found.get(watch["asin"])
                record(watch, [listing] if listing else [])

    for watch in (w for w in watches if w["book_id"] and not w["asin"]):
        book = db.get_book(conn, watch["book_id"])
        if book is None:
            continue
        try:
            listings = provider.search(search_query(book["title"]), limit=10)
        except (HttpError, ProviderError, OSError, ValueError) as exc:
            failed(watch, exc)
            continue
        record(watch, [item for item in listings if title_match(book["title"], item.title)])
    return changes
