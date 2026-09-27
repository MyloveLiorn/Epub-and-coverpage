"""SQLite storage for the agency map, discovered books, market checks and tracking snapshots."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from govbooks.models import Agency

SCHEMA = """
CREATE TABLE IF NOT EXISTS agencies (
    id TEXT PRIMARY KEY,
    level TEXT NOT NULL,
    jurisdiction TEXT NOT NULL,
    name TEXT NOT NULL,
    short_name TEXT,
    parent_id TEXT,
    website TEXT,
    description TEXT,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agencies_parent ON agencies (parent_id);

CREATE TABLE IF NOT EXISTS books (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    subtitle TEXT,
    authors TEXT NOT NULL DEFAULT '[]',
    publisher TEXT,
    year INTEGER,
    subjects TEXT NOT NULL DEFAULT '[]',
    description TEXT,
    isbns TEXT NOT NULL DEFAULT '[]',
    agency_id TEXT,
    level TEXT,
    jurisdiction TEXT,
    rights TEXT NOT NULL,
    rights_note TEXT,
    fulltext_url TEXT,
    sources TEXT NOT NULL DEFAULT '[]',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS book_topics (
    book_id TEXT NOT NULL REFERENCES books (id),
    topic TEXT NOT NULL,
    score INTEGER NOT NULL,
    PRIMARY KEY (book_id, topic)
);

CREATE TABLE IF NOT EXISTS market_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL REFERENCES books (id),
    provider TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    query TEXT NOT NULL,
    matching_listings INTEGER NOT NULL,
    best_rank INTEGER,
    median_price REAL,
    topic_rank INTEGER,
    verdict TEXT NOT NULL,
    score INTEGER NOT NULL,
    listings TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS market_checks_book ON market_checks (book_id, checked_at);

CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT REFERENCES books (id),
    asin TEXT,
    label TEXT NOT NULL,
    added_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id INTEGER NOT NULL REFERENCES watchlist (id),
    taken_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    title TEXT,
    sales_rank INTEGER,
    price REAL,
    listing_count INTEGER,
    asins TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS snapshots_watch ON snapshots (watch_id, taken_at);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Columns added after the first release, so older databases are upgraded in place.
MIGRATIONS = {"books": {"jurisdiction": "TEXT"}}


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for table, columns in MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for column, spec in columns.items():
            if existing and column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
    conn.executescript(SCHEMA)
    return conn


def loads(value: str | None) -> Any:
    return json.loads(value) if value else []


# --- agencies ---------------------------------------------------------------


def upsert_agencies(conn: sqlite3.Connection, agencies: Iterable[Agency]) -> int:
    stamp = now()
    rows = [
        (a.id, a.level, a.jurisdiction, a.name, a.short_name, a.parent_id, a.website, a.description, a.source, stamp)
        for a in agencies
    ]
    conn.executemany(
        """
        INSERT INTO agencies (id, level, jurisdiction, name, short_name, parent_id, website, description, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            level = excluded.level, jurisdiction = excluded.jurisdiction, name = excluded.name,
            short_name = excluded.short_name, parent_id = excluded.parent_id, website = excluded.website,
            description = excluded.description, source = excluded.source, updated_at = excluded.updated_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def all_agencies(conn: sqlite3.Connection) -> list[Agency]:
    rows = conn.execute(
        "SELECT id, level, jurisdiction, name, short_name, parent_id, website, description, source FROM agencies"
    ).fetchall()
    return [Agency(**dict(r)) for r in rows]


def get_agency(conn: sqlite3.Connection, agency_id: str) -> Agency | None:
    row = conn.execute(
        "SELECT id, level, jurisdiction, name, short_name, parent_id, website, description, source"
        " FROM agencies WHERE id = ?",
        (agency_id,),
    ).fetchone()
    return Agency(**dict(row)) if row else None


# --- books ------------------------------------------------------------------


def get_book(conn: sqlite3.Connection, book_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()


def save_book(conn: sqlite3.Connection, book: dict) -> None:
    columns = [
        "id", "title", "subtitle", "authors", "publisher", "year", "subjects", "description", "isbns",
        "agency_id", "level", "jurisdiction", "rights", "rights_note", "fulltext_url", "sources", "first_seen",
        "last_seen",
    ]  # fmt: skip
    values = [json.dumps(book[c]) if isinstance(book[c], list) else book[c] for c in columns]
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in ("id", "first_seen"))
    conn.execute(
        f"INSERT INTO books ({', '.join(columns)}) VALUES ({placeholders})"
        f" ON CONFLICT (id) DO UPDATE SET {updates}",
        values,
    )


def set_book_topic(conn: sqlite3.Connection, book_id: str, topic: str, score: int) -> None:
    conn.execute(
        """
        INSERT INTO book_topics (book_id, topic, score) VALUES (?, ?, ?)
        ON CONFLICT (book_id, topic) DO UPDATE SET score = MAX(score, excluded.score)
        """,
        (book_id, topic, score),
    )


def list_books(
    conn: sqlite3.Connection,
    topic: str | None = None,
    min_score: int = 0,
    rights: Iterable[str] | None = None,
    level: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Books with their best topic score and latest market check, best first."""
    where = ["t.score >= ?"]
    params: list[Any] = [min_score]
    if topic:
        where.append("t.topic = ?")
        params.append(topic)
    if rights:
        rights = list(rights)
        where.append(f"b.rights IN ({', '.join('?' for _ in rights)})")
        params.extend(rights)
    if level:
        where.append("b.level = ?")
        params.append(level)
    sql = f"""
        SELECT b.*, MAX(t.score) AS topic_score, GROUP_CONCAT(DISTINCT t.topic) AS topics,
               a.name AS agency_name,
               m.verdict, m.score AS market_score, m.best_rank, m.median_price,
               m.matching_listings, m.checked_at, m.provider
        FROM books b
        JOIN book_topics t ON t.book_id = b.id
        LEFT JOIN agencies a ON a.id = b.agency_id
        LEFT JOIN market_checks m ON m.id = (
            SELECT id FROM market_checks WHERE book_id = b.id ORDER BY checked_at DESC, id DESC LIMIT 1
        )
        WHERE {' AND '.join(where)}
        GROUP BY b.id
        ORDER BY COALESCE(m.score, -1) DESC, topic_score DESC, b.year DESC
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


# --- market checks ----------------------------------------------------------


def add_market_check(conn: sqlite3.Connection, check: dict) -> None:
    conn.execute(
        """
        INSERT INTO market_checks (book_id, provider, checked_at, query, matching_listings, best_rank,
                                   median_price, topic_rank, verdict, score, listings)
        VALUES (:book_id, :provider, :checked_at, :query, :matching_listings, :best_rank,
                :median_price, :topic_rank, :verdict, :score, :listings)
        """,
        {**check, "listings": json.dumps(check["listings"])},
    )
    conn.commit()


# --- watchlist and snapshots --------------------------------------------------


def add_watch(conn: sqlite3.Connection, label: str, book_id: str | None = None, asin: str | None = None) -> int:
    existing = conn.execute(
        "SELECT id FROM watchlist WHERE book_id IS ? AND asin IS ?", (book_id, asin)
    ).fetchone()
    if existing:
        conn.execute("UPDATE watchlist SET active = 1 WHERE id = ?", (existing["id"],))
        conn.commit()
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO watchlist (book_id, asin, label, added_at) VALUES (?, ?, ?, ?)",
        (book_id, asin, label, now()),
    )
    conn.commit()
    return cur.lastrowid


def list_watches(conn: sqlite3.Connection, active_only: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM watchlist"
    if active_only:
        sql += " WHERE active = 1"
    return conn.execute(sql + " ORDER BY id").fetchall()


def deactivate_watch(conn: sqlite3.Connection, watch_id: int) -> bool:
    cur = conn.execute("UPDATE watchlist SET active = 0 WHERE id = ?", (watch_id,))
    conn.commit()
    return cur.rowcount > 0


def add_snapshot(conn: sqlite3.Connection, snap: dict) -> None:
    conn.execute(
        """
        INSERT INTO snapshots (watch_id, taken_at, provider, title, sales_rank, price, listing_count, asins)
        VALUES (:watch_id, :taken_at, :provider, :title, :sales_rank, :price, :listing_count, :asins)
        """,
        {**snap, "asins": json.dumps(snap.get("asins", []))},
    )
    conn.commit()


def snapshots_for(conn: sqlite3.Connection, watch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM snapshots WHERE watch_id = ? ORDER BY taken_at, id", (watch_id,)
    ).fetchall()
