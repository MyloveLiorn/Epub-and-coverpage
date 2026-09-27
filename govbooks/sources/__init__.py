"""Places to search for government-published books.

Each source yields Records for a keyword. With ``author`` set, it searches for works by
that agency; without it, it searches for works from government publishers in general.
"""

from __future__ import annotations

from typing import Iterator, Protocol

from govbooks.config import Config, env
from govbooks.http import Http
from govbooks.models import Record


class Source(Protocol):
    name: str

    def search(self, keyword: str, author: str | None = None, limit: int = 100) -> Iterator[Record]: ...


def quote(value: str) -> str:
    return '"' + value.replace('"', " ").strip() + '"'


def build_sources(names: list[str], http: Http, config: Config) -> list[Source]:
    from govbooks.sources.google_books import GoogleBooksSource
    from govbooks.sources.govinfo import GovInfoSource
    from govbooks.sources.internet_archive import InternetArchiveSource
    from govbooks.sources.open_library import OpenLibrarySource

    factories = {
        "govinfo": lambda: GovInfoSource(http, env("GOVINFO_API_KEY", "DEMO_KEY"), config.govinfo_collections),
        "internet_archive": lambda: InternetArchiveSource(http, config.internet_archive_collections),
        "open_library": lambda: OpenLibrarySource(http),
        "google_books": lambda: GoogleBooksSource(http, env("GOOGLE_BOOKS_API_KEY")),
    }
    unknown = [n for n in names if n not in factories]
    if unknown:
        raise SystemExit(f"Unknown source(s): {', '.join(unknown)}. Available: {', '.join(factories)}")
    return [factories[n]() for n in names]
