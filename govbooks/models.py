"""Plain data objects shared across the package."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Agency:
    """One office in the government map (a federal agency, a state agency, or a synthetic root)."""

    id: str
    level: str  # "federal" or "state"
    jurisdiction: str  # "United States" or a state name
    name: str
    short_name: str | None = None
    parent_id: str | None = None
    website: str | None = None
    description: str | None = None
    source: str = ""


@dataclass
class Record:
    """One publication as returned by a single search source."""

    source: str
    source_id: str
    title: str
    subtitle: str | None = None
    authors: list[str] = field(default_factory=list)
    publisher: str | None = None
    year: int | None = None
    subjects: list[str] = field(default_factory=list)
    description: str | None = None
    isbns: list[str] = field(default_factory=list)
    url: str | None = None
    fulltext_url: str | None = None
    # Some sources state outright that a work is public domain (Google Books does).
    public_domain: bool | None = None


@dataclass
class Listing:
    """One product on Amazon (or, for the catalog provider, one known commercial edition)."""

    asin: str
    title: str
    sales_rank: int | None = None
    price: float | None = None
    url: str | None = None
    authors: list[str] = field(default_factory=list)
    binding: str | None = None
