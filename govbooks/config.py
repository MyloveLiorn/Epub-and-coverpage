"""Loading govbooks.toml. API keys come from environment variables, never from the file."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("govbooks.toml")

DEFAULT_SOURCES = ["govinfo", "internet_archive", "open_library", "google_books"]


@dataclass
class Topic:
    name: str
    keywords: list[str]
    exclude: list[str] = field(default_factory=list)
    # A keyword hit in the title scores 3, in the subjects 2, in the description 1.
    min_score: int = 3


@dataclass
class Config:
    database: Path = Path("govbooks.db")
    topics: dict[str, Topic] = field(default_factory=dict)
    sources: list[str] = field(default_factory=lambda: list(DEFAULT_SOURCES))
    max_results: int = 100
    govinfo_collections: list[str] = field(default_factory=lambda: ["GPO", "CPRT", "GAOREPORTS", "ERP"])
    internet_archive_collections: list[str] = field(
        default_factory=lambda: ["usgovernmentdocuments", "fedlink"]
    )
    states: list[str] = field(default_factory=list)  # empty means all 50
    amazon_provider: str = "catalog"
    marketplace: str = "www.amazon.com"

    def topic(self, name: str) -> Topic:
        try:
            return self.topics[name]
        except KeyError:
            known = ", ".join(sorted(self.topics)) or "none"
            raise SystemExit(f"Unknown topic {name!r}. Topics in the config: {known}") from None


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return Config()
    raw = tomllib.loads(path.read_text())
    sources = raw.get("sources", {})
    amazon = raw.get("amazon", {})
    topics = {
        name: Topic(
            name=name,
            keywords=list(spec.get("keywords", [])),
            exclude=list(spec.get("exclude", [])),
            min_score=int(spec.get("min_score", 3)),
        )
        for name, spec in raw.get("topics", {}).items()
    }
    for topic in topics.values():
        if not topic.keywords:
            raise SystemExit(f"Topic {topic.name!r} in {path} has no keywords.")
    defaults = Config()
    database = Path(raw.get("database", defaults.database))
    if not database.is_absolute():
        database = path.parent / database
    return Config(
        database=database,
        topics=topics,
        sources=list(sources.get("enabled", defaults.sources)),
        max_results=int(sources.get("max_results_per_query", defaults.max_results)),
        govinfo_collections=list(sources.get("govinfo_collections", defaults.govinfo_collections)),
        internet_archive_collections=list(
            sources.get("internet_archive_collections", defaults.internet_archive_collections)
        ),
        states=list(raw.get("agencies", {}).get("states", [])),
        amazon_provider=amazon.get("provider", defaults.amazon_provider),
        marketplace=amazon.get("marketplace", defaults.marketplace),
    )


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default
