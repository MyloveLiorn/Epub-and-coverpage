"""SQLite storage for the .gov site map and what crawling found on each site."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from govweb.classify import title_from_url
from govweb.crawl import SiteResult
from govweb.registry import Site

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    domain TEXT PRIMARY KEY,
    domain_type TEXT NOT NULL,
    level TEXT NOT NULL,
    election INTEGER NOT NULL DEFAULT 0,
    organization TEXT NOT NULL,
    suborganization TEXT,
    city TEXT,
    state TEXT,
    -- Set for sub-sites found by crawling (water.ca.gov under ca.gov); NULL for registry domains.
    parent_domain TEXT,
    in_registry INTEGER NOT NULL DEFAULT 1,
    synced_at TEXT NOT NULL,
    crawled_at TEXT,
    crawl_status TEXT,
    home_url TEXT,
    pages_crawled INTEGER,
    documents_found INTEGER,
    sitemap_urls INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS sites_level ON sites (level, state);

CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY,
    domain TEXT NOT NULL REFERENCES sites (domain),
    title TEXT,
    depth INTEGER,
    status INTEGER,
    hint_score INTEGER NOT NULL DEFAULT 0,
    found_via TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pages_domain ON pages (domain);

CREATE TABLE IF NOT EXISTS documents (
    url TEXT PRIMARY KEY,
    domain TEXT NOT NULL REFERENCES sites (domain),
    title TEXT NOT NULL,
    file_type TEXT NOT NULL,
    found_on TEXT,
    found_via TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_domain ON documents (domain);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def sync_sites(conn: sqlite3.Connection, sites: Iterable[Site]) -> tuple[int, int]:
    """Upsert the registry. Domains that left the registry are kept but flagged. Returns (total, new)."""
    stamp = now()
    before = {r[0] for r in conn.execute("SELECT domain FROM sites")}
    rows = [
        (s.domain, s.domain_type, s.level, int(s.election), s.organization, s.suborganization, s.city, s.state, stamp)
        for s in sites
    ]
    conn.executemany(
        """
        INSERT INTO sites (domain, domain_type, level, election, organization, suborganization, city, state, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (domain) DO UPDATE SET
            domain_type = excluded.domain_type, level = excluded.level, election = excluded.election,
            organization = excluded.organization, suborganization = excluded.suborganization,
            city = excluded.city, state = excluded.state, in_registry = 1, synced_at = excluded.synced_at
        """,
        rows,
    )
    conn.execute("UPDATE sites SET in_registry = 0 WHERE parent_domain IS NULL AND synced_at < ?", (stamp,))
    conn.commit()
    return len(rows), len({r[0] for r in rows} - before)


def registered_domain_of(conn: sqlite3.Connection, host: str) -> sqlite3.Row | None:
    """The registry row a host belongs to: water.ca.gov -> ca.gov."""
    labels = host.lower().removeprefix("www.").split(".")
    for start in range(len(labels) - 1):
        row = conn.execute(
            "SELECT * FROM sites WHERE domain = ? AND parent_domain IS NULL", (".".join(labels[start:]),)
        ).fetchone()
        if row:
            return row
    return None


def add_subsites(conn: sqlite3.Connection, parent: sqlite3.Row, hosts: Iterable[str]) -> int:
    """Record subdomains of a registry domain as sites of their own, inheriting its owner."""
    rows = [
        (host, parent["domain_type"], parent["level"], parent["election"], parent["organization"],
         parent["suborganization"], parent["city"], parent["state"], parent["domain"], now())
        for host in hosts
        if host != parent["domain"] and host.endswith("." + parent["domain"])
    ]  # fmt: skip
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO sites (domain, domain_type, level, election, organization, suborganization, city,
                                     state, parent_domain, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return conn.total_changes - before


def select_sites(
    conn: sqlite3.Connection,
    domains: list[str] | None = None,
    level: str | None = None,
    state: str | None = None,
    search: str | None = None,
    include_election: bool = False,
    not_crawled_since: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    where, params = ["in_registry = 1"], []
    if domains:
        where.append(f"domain IN ({', '.join('?' for _ in domains)})")
        params.extend(d.lower() for d in domains)
    if level:
        where.append("level = ?")
        params.append(level)
    if state:
        where.append("state = ?")
        params.append(state.upper())
    if search:
        where.append("(organization LIKE ? OR suborganization LIKE ? OR domain LIKE ?)")
        params.extend([f"%{search}%"] * 3)
    if not include_election and not domains:
        where.append("election = 0")
    if not_crawled_since:
        where.append("(crawled_at IS NULL OR crawled_at < ?)")
        params.append(not_crawled_since)
    sql = f"SELECT * FROM sites WHERE {' AND '.join(where)} ORDER BY level, state, organization, domain"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def store_result(conn: sqlite3.Connection, result: SiteResult) -> int:
    """Save a crawl. Returns the number of new sub-sites it discovered."""
    stamp = now()
    conn.executemany(
        """
        INSERT INTO pages (url, domain, title, depth, status, hint_score, found_via, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (url) DO UPDATE SET title = excluded.title, depth = MIN(depth, excluded.depth),
            status = excluded.status, hint_score = excluded.hint_score, fetched_at = excluded.fetched_at
        """,
        [(p.url, result.domain, p.title, p.depth, p.status, p.hint_score, p.found_via, stamp) for p in result.pages],
    )
    conn.executemany(
        """
        INSERT INTO documents (url, domain, title, file_type, found_on, found_via, first_seen, last_seen)
        VALUES (:url, :domain, :title, :file_type, :found_on, :found_via, :stamp, :stamp)
        ON CONFLICT (url) DO UPDATE SET last_seen = excluded.last_seen,
            title = CASE WHEN documents.title = :fallback THEN excluded.title ELSE documents.title END
        """,
        [
            {**vars(d), "domain": result.domain, "stamp": stamp, "fallback": title_from_url(d.url)}
            for d in result.documents.values()
        ],
    )
    total_docs = conn.execute("SELECT COUNT(*) FROM documents WHERE domain = ?", (result.domain,)).fetchone()[0]
    conn.execute(
        """
        UPDATE sites SET crawled_at = ?, crawl_status = ?, home_url = ?, pages_crawled = ?, documents_found = ?,
                         sitemap_urls = ?, error = ?
        WHERE domain = ?
        """,
        (stamp, result.status, result.home_url, len(result.pages), total_docs, result.sitemap_urls,
         result.error, result.domain),
    )  # fmt: skip
    conn.commit()
    parent = registered_domain_of(conn, result.domain)
    return add_subsites(conn, parent, result.other_hosts) if parent and result.other_hosts else 0


def select_documents(
    conn: sqlite3.Connection,
    domain: str | None = None,
    level: str | None = None,
    state: str | None = None,
    file_type: str | None = None,
    search: str | None = None,
) -> list[sqlite3.Row]:
    where, params = ["1 = 1"], []
    for column, value in (("d.domain", domain), ("s.level", level), ("s.state", state and state.upper()),
                          ("d.file_type", file_type)):  # fmt: skip
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if search:
        where.append("(d.title LIKE ? OR d.url LIKE ?)")
        params.extend([f"%{search}%"] * 2)
    sql = f"""
        SELECT d.*, s.organization, s.suborganization, s.level, s.state
        FROM documents d JOIN sites s ON s.domain = d.domain
        WHERE {' AND '.join(where)}
        ORDER BY s.level, s.organization, d.title
    """
    return conn.execute(sql, params).fetchall()


def stats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT level, SUM(parent_domain IS NULL) AS sites, SUM(parent_domain IS NOT NULL) AS subsites,
               SUM(crawled_at IS NOT NULL) AS crawled, SUM(COALESCE(documents_found, 0)) AS documents
        FROM sites WHERE in_registry = 1 GROUP BY level ORDER BY sites DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]
