"""The market provider interface and how a book's Amazon situation is scored."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Protocol

from govbooks.models import Listing
from govbooks.text import title_match


class ProviderError(RuntimeError):
    pass


class MarketProvider(Protocol):
    name: str
    # False for providers that cannot see Amazon sales ranks (the catalog fallback).
    has_sales_rank: bool

    def search(self, query: str, limit: int = 10) -> list[Listing]: ...

    def lookup(self, asins: list[str]) -> list[Listing]: ...


RIGHTS_FACTOR = {"public_domain": 1.0, "likely_public_domain": 0.9, "check": 0.6, "unknown": 0.4}


@dataclass
class MarketResult:
    matching: list[Listing] = field(default_factory=list)
    best_rank: int | None = None
    median_price: float | None = None
    topic_rank: int | None = None
    verdict: str = ""
    score: int = 0


def demand_points(rank: int | None) -> int:
    """Amazon Books sales rank to 0-60 points. Lower rank means more sales."""
    if rank is None:
        return 20
    for limit, points in ((50_000, 60), (200_000, 45), (500_000, 30), (1_000_000, 15)):
        if rank <= limit:
            return points
    return 5


def competition_points(matches: int) -> int:
    """Fewer existing editions of the same book leave more room: 0-40 points."""
    for limit, points in ((0, 40), (1, 28), (3, 18), (6, 8)):
        if matches <= limit:
            return points
    return 0


def verdict_for(matches: int, best_rank: int | None, topic_rank: int | None, has_rank: bool) -> str:
    if matches == 0:
        return "open_gap" if topic_rank is not None and topic_rank <= 500_000 else "untested_gap"
    if matches >= 4:
        return "crowded"
    if not has_rank:
        return "some_competition"
    return "proven_demand" if best_rank is not None and best_rank <= 300_000 else "low_demand"


VERDICT_HELP = {
    "open_gap": "No edition on Amazon, and books on this topic sell.",
    "untested_gap": "No edition on Amazon; topic demand unknown or weak.",
    "proven_demand": "A few editions exist and at least one sells well.",
    "low_demand": "A few editions exist but none sells well.",
    "some_competition": "A few editions exist (sales rank not available from this provider).",
    "crowded": "Four or more editions already on Amazon.",
}


def evaluate(
    title: str, listings: list[Listing], topic_rank: int | None, rights: str, has_rank: bool = True
) -> MarketResult:
    matching = [listing for listing in listings if title_match(title, listing.title)]
    ranks = sorted(item.sales_rank for item in matching if item.sales_rank)
    prices = [item.price for item in matching if item.price]
    best_rank = ranks[0] if ranks else None
    reference_rank = best_rank if matching else topic_rank
    raw = demand_points(reference_rank) + competition_points(len(matching))
    return MarketResult(
        matching=matching,
        best_rank=best_rank,
        median_price=round(statistics.median(prices), 2) if prices else None,
        topic_rank=topic_rank,
        verdict=verdict_for(len(matching), best_rank, topic_rank, has_rank),
        score=round(raw * RIGHTS_FACTOR.get(rights, 0.4)),
    )


def topic_demand(provider: MarketProvider, keywords: list[str], per_keyword: int = 10) -> int | None:
    """Median sales rank of the top results for the topic's first keywords."""
    if not provider.has_sales_rank:
        return None
    ranks: list[int] = []
    for keyword in keywords[:2]:
        ranks.extend(item.sales_rank for item in provider.search(keyword, limit=per_keyword) if item.sales_rank)
    return int(statistics.median(sorted(ranks)[:10])) if ranks else None
