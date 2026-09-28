"""Finding topic-relevant government books across sources and storing them as one row per edition."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from govbooks import copyright_policy, db
from govbooks.agencies import AgencyIndex, AgencyMatch
from govbooks.config import Topic
from govbooks.http import HttpError
from govbooks.models import Agency, Record
from govbooks.sources import Source
from govbooks.text import contains_phrase, normalize_text, work_key

# A keyword found in the title counts most, then subjects, then the description.
TITLE_WEIGHT, SUBJECT_WEIGHT, DESCRIPTION_WEIGHT = 3, 2, 1
# After this many failures in a row that won't clear up soon (network down, quota used up,
# access refused), a source is skipped for the rest of the run.
MAX_NETWORK_FAILURES = 3
BLOCKING_STATUSES = {None, 401, 403, 429}


def score_topic(topic: Topic, record: Record) -> int:
    title = normalize_text(f"{record.title} {record.subtitle or ''}")
    subjects = normalize_text(" ; ".join(record.subjects))
    description = normalize_text(record.description)
    for word in topic.exclude:
        phrase = normalize_text(word)
        if any(contains_phrase(text, phrase) for text in (title, subjects, description)):
            return 0
    score = 0
    for keyword in topic.keywords:
        phrase = normalize_text(keyword)
        score += TITLE_WEIGHT * contains_phrase(title, phrase)
        score += SUBJECT_WEIGHT * contains_phrase(subjects, phrase)
        score += DESCRIPTION_WEIGHT * contains_phrase(description, phrase)
    return score


def assess_rights(
    level: str | None,
    year: int | None,
    source_says_pd: bool | None,
    today: date | None = None,
    jurisdiction: str | None = None,
    names: list[str | None] | tuple = (),
) -> tuple[str, str]:
    """A first-pass copyright call (see govbooks.copyright_policy). A screening aid, not legal advice."""
    rights = copyright_policy.assess(
        level, state=jurisdiction, names=names, year=year, source_says_pd=source_says_pd, today=today
    )
    return rights.status, rights.note


@dataclass
class DiscoverStats:
    queries: int = 0
    records: int = 0
    kept: int = 0
    new_books: int = 0
    errors: list[str] = field(default_factory=list)


def _merge(existing: sqlite3.Row | None, record: Record, match: AgencyMatch, stamp: str) -> dict:
    source_ref = {"source": record.source, "id": record.source_id, "url": record.url}
    if existing is None:
        book = {
            "id": work_key(record.title, record.year),
            "title": record.title,
            "subtitle": record.subtitle,
            "authors": record.authors,
            "publisher": record.publisher,
            "year": record.year,
            "subjects": record.subjects,
            "description": record.description,
            "isbns": record.isbns,
            "agency_id": match.agency_id,
            "level": match.level,
            "jurisdiction": match.jurisdiction,
            "fulltext_url": record.fulltext_url,
            "sources": [source_ref],
            "first_seen": stamp,
            "pd_flag": record.public_domain,
        }
    else:
        book = dict(existing)
        for key in ("authors", "subjects", "isbns", "sources"):
            book[key] = db.loads(book[key])
        for key in ("subtitle", "publisher", "description", "fulltext_url"):
            book[key] = book[key] or getattr(record, key)
        book["authors"] = list(dict.fromkeys(book["authors"] + record.authors))
        book["subjects"] = list(dict.fromkeys(book["subjects"] + record.subjects))[:40]
        book["isbns"] = list(dict.fromkeys(book["isbns"] + record.isbns))[:20]
        if source_ref not in book["sources"]:
            book["sources"].append(source_ref)
        # A named agency beats a publisher-only guess.
        if match.agency_id and not book["agency_id"]:
            book["agency_id"], book["level"], book["jurisdiction"] = match.agency_id, match.level, match.jurisdiction
        book["jurisdiction"] = book["jurisdiction"] or match.jurisdiction
        book["pd_flag"] = record.public_domain or book["rights"] == "public_domain"
    names = [*book["authors"], book["publisher"], match.name]
    book["rights"], book["rights_note"] = assess_rights(
        book["level"], book["year"], book.pop("pd_flag"), jurisdiction=book["jurisdiction"], names=names
    )
    book["last_seen"] = stamp
    return book


def store_record(
    conn: sqlite3.Connection, topic: Topic, record: Record, score: int, match: AgencyMatch
) -> bool:
    """Save a record as (part of) a book. Returns True if the book is new."""
    book_id = work_key(record.title, record.year)
    existing = db.get_book(conn, book_id)
    book = _merge(existing, record, match, db.now())
    db.save_book(conn, book)
    db.set_book_topic(conn, book_id, topic.name, score)
    return existing is None


def discover(
    conn: sqlite3.Connection,
    topic: Topic,
    sources: list[Source],
    index: AgencyIndex,
    agencies: list[Agency] | None = None,
    limit: int = 100,
    progress: Callable[[str], None] | None = None,
) -> DiscoverStats:
    """Search every source for every topic keyword and keep the relevant government books.

    With ``agencies`` given, each source is searched once per agency (by author) instead of
    for government publishers in general; this is how state offices get covered.
    """
    stats = DiscoverStats()
    authors: list[Agency | None] = list(agencies) if agencies else [None]
    network_failures: dict[str, int] = {}
    for keyword in topic.keywords:
        for source in sources:
            for agency in authors:
                if network_failures.get(source.name, 0) >= MAX_NETWORK_FAILURES:
                    break
                label = f"{source.name}: {keyword!r}" + (f" by {agency.name}" if agency else "")
                stats.queries += 1
                found = kept = 0
                try:
                    for record in source.search(keyword, author=agency.name if agency else None, limit=limit):
                        found += 1
                        score = score_topic(topic, record)
                        if score < topic.min_score:
                            continue
                        match = index.match([*record.authors, record.publisher])
                        if match is None and agency:
                            match = AgencyMatch(agency.id, agency.level, agency.jurisdiction, agency.name)
                        if match is None:  # not a government publication
                            continue
                        kept += 1
                        stats.new_books += store_record(conn, topic, record, score, match)
                except (HttpError, OSError, ValueError) as exc:
                    stats.errors.append(f"{label}: {exc}")
                    if progress:
                        progress(f"  {label}: failed ({exc})")
                    if isinstance(exc, HttpError) and exc.status in BLOCKING_STATUSES:
                        network_failures[source.name] = network_failures.get(source.name, 0) + 1
                        if network_failures[source.name] == MAX_NETWORK_FAILURES and progress:
                            progress(f"  {source.name}: unreachable or over its limit, skipping its remaining queries")
                    continue
                finally:
                    conn.commit()
                network_failures[source.name] = 0
                stats.records += found
                stats.kept += kept
                if progress:
                    progress(f"  {label}: {found} results, {kept} kept")
    return stats
