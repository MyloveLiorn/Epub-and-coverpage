"""SQLite storage for the site map, the agency directory, what crawling found, and watches."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from govweb.classify import book_score, clean_title, is_generic_title, meaningful_file_name, title_from_url
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
    -- Set for sub-sites (water.ca.gov under ca.gov); NULL otherwise.
    parent_domain TEXT,
    -- registry (the .gov list), subdomain (found under a registry domain), agency (an agency
    -- directory website outside the registry, e.g. army.mil) or manual (added by hand).
    source TEXT NOT NULL DEFAULT 'registry',
    in_registry INTEGER NOT NULL DEFAULT 1,  -- 0 once a registry domain leaves the registry
    synced_at TEXT NOT NULL,
    first_crawled_at TEXT,
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
    book_score INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_domain ON documents (domain);
CREATE INDEX IF NOT EXISTS documents_first_seen ON documents (first_seen);

-- Federal (Federal Register) and state (Wikidata) agencies with their websites.
-- Ids match govbooks' agency ids, so the two tools can be joined later.
CREATE TABLE IF NOT EXISTS agencies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    short_name TEXT,
    level TEXT NOT NULL,
    state TEXT,
    parent_id TEXT,
    website TEXT,
    host TEXT,
    synced_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agencies_host ON agencies (host);

-- Whether each book-like document is already sold on Amazon.
CREATE TABLE IF NOT EXISTS amazon_checks (
    url TEXT PRIMARY KEY REFERENCES documents (url),
    checked_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    query TEXT NOT NULL,
    matching INTEGER NOT NULL,
    asins TEXT NOT NULL DEFAULT '[]',
    best_rank INTEGER,
    price REAL
);

-- Saved searches for new books.
CREATE TABLE IF NOT EXISTS watches (
    name TEXT PRIMARY KEY,
    topic TEXT,
    keywords TEXT,
    level TEXT,
    state TEXT,
    domain TEXT,
    search TEXT,
    books_only INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_run_at TEXT
);
"""

# Columns added after the first release, so older databases can be upgraded in place.
MIGRATIONS = {
    "sites": {
        "source": "TEXT NOT NULL DEFAULT 'registry'",
        "first_crawled_at": "TEXT",
    },
    "documents": {"book_score": "INTEGER NOT NULL DEFAULT 0"},
}


# Changes to stored data that go with a code change, tracked in SQLite's user_version.
# 1: document titles cleaned (HTML entities, text said twice, trailing dots).
# 2: "Download »"-style titles replaced by the file name; Amazon matches re-checked with the
#    stricter rules for short titles.
DATA_VERSION = 2


def now() -> str:
    # Microseconds keep "found after the last run" exact even for back-to-back runs. ISO strings
    # still sort correctly against older second-precision values ("...:00+00:00" < "...:00.5+00:00").
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    conn.executescript(SCHEMA)
    _upgrade_data(conn)
    return conn


def _upgrade_data(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        rows = conn.execute("SELECT url, title FROM documents").fetchall()
        cleaned = [(clean_title(r["title"]) or r["title"], r["url"]) for r in rows]
        conn.executemany(
            "UPDATE documents SET title = ?, book_score = ? WHERE url = ?",
            [(title, book_score(title, url), url) for (title, url), r in zip(cleaned, rows) if title != r["title"]],
        )
    if version < 2:
        rows = conn.execute("SELECT url, title FROM documents").fetchall()
        renamed = [(title_from_url(r["url"]), r["url"]) for r in rows
                   if is_generic_title(r["title"]) and meaningful_file_name(r["url"])]  # fmt: skip
        conn.executemany(
            "UPDATE documents SET title = ?, book_score = ? WHERE url = ?",
            [(title, book_score(title, url), url) for title, url in renamed],
        )
        conn.execute("DELETE FROM amazon_checks WHERE matching > 0")
    if version < DATA_VERSION:
        conn.execute(f"PRAGMA user_version = {DATA_VERSION}")
        conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:  # a new database; the schema creates everything
            continue
        for column, spec in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
                if (table, column) == ("sites", "source"):
                    conn.execute("UPDATE sites SET source = 'subdomain' WHERE parent_domain IS NOT NULL")
                if (table, column) == ("sites", "first_crawled_at"):
                    conn.execute("UPDATE sites SET first_crawled_at = crawled_at")
                if (table, column) == ("documents", "book_score"):
                    rows = conn.execute("SELECT url, title FROM documents").fetchall()
                    conn.executemany(
                        "UPDATE documents SET book_score = ? WHERE url = ?",
                        [(book_score(r["title"], r["url"]), r["url"]) for r in rows],
                    )
    conn.commit()


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
    conn.execute("UPDATE sites SET in_registry = 0 WHERE source = 'registry' AND synced_at < ?", (stamp,))
    conn.commit()
    return len(rows), len({r[0] for r in rows} - before)


def get_site(conn: sqlite3.Connection, domain: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sites WHERE domain = ?", (domain,)).fetchone()


def remove_sites(conn: sqlite3.Connection, domains: list[str]) -> int:
    """Take sites off the map with everything found on them."""
    for domain in domains:
        conn.execute(
            "DELETE FROM amazon_checks WHERE url IN (SELECT url FROM documents WHERE domain = ?)", (domain,)
        )
        for table in ("documents", "pages"):
            conn.execute(f"DELETE FROM {table} WHERE domain = ?", (domain,))
        conn.execute("DELETE FROM sites WHERE domain = ? OR parent_domain = ?", (domain, domain))
    conn.commit()
    return len(domains)


def registered_domain_of(conn: sqlite3.Connection, host: str) -> sqlite3.Row | None:
    """The top-level site a host belongs to: water.ca.gov -> ca.gov (itself if it is one)."""
    labels = host.lower().removeprefix("www.").split(".")
    for start in range(len(labels) - 1):
        row = conn.execute(
            "SELECT * FROM sites WHERE domain = ? AND parent_domain IS NULL", (".".join(labels[start:]),)
        ).fetchone()
        if row:
            return row
    return None


def add_site(
    conn: sqlite3.Connection,
    domain: str,
    organization: str,
    level: str = "other",
    state: str | None = None,
    suborganization: str | None = None,
    source: str = "manual",
    domain_type: str = "Added by hand",
) -> bool:
    """Add a site outside the registry. Returns False if it already exists."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO sites (domain, domain_type, level, organization, suborganization, state, source,
                                     synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (domain, domain_type, level, organization, suborganization, state, source, now()),
    )
    conn.commit()
    return cur.rowcount > 0


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
                                     state, parent_domain, source, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'subdomain', ?)
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
    crawled_only: bool = False,
    limit: int | None = None,
    rotation: bool = False,
) -> list[sqlite3.Row]:
    """Sites matching the filters. With rotation, never-crawled sites come first, then the ones
    crawled longest ago, so capped runs work through the whole map over time."""
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
    if crawled_only:
        where.append("crawled_at IS NOT NULL")
    order = "level, state, organization, domain"
    if rotation:
        order = "crawled_at IS NOT NULL, crawled_at, source != 'registry', " + order
    sql = f"SELECT * FROM sites WHERE {' AND '.join(where)} ORDER BY {order}"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def select_subsites(
    conn: sqlite3.Connection, parents: list[str], not_crawled_since: str | None = None
) -> list[sqlite3.Row]:
    """Sub-sites found under the given sites (history.army.mil under army.mil), never-crawled
    ones first, then those crawled longest ago."""
    if not parents:
        return []
    where = ["in_registry = 1", f"parent_domain IN ({', '.join('?' for _ in parents)})"]
    params: list = list(parents)
    if not_crawled_since:
        where.append("(crawled_at IS NULL OR crawled_at < ?)")
        params.append(not_crawled_since)
    sql = f"SELECT * FROM sites WHERE {' AND '.join(where)} ORDER BY crawled_at IS NOT NULL, crawled_at, domain"
    return conn.execute(sql, params).fetchall()


def store_result(conn: sqlite3.Connection, result: SiteResult) -> int:
    """Save a crawl. Returns the number of new sub-sites it discovered.

    Only pages worth revisiting are kept (home pages, publication listings and pages that linked
    to documents), so the database stays small enough to carry from run to run."""
    stamp = now()
    linked = {d.found_on for d in result.documents.values()}
    pages = [p for p in result.pages if p.depth == 0 or p.hint_score > 0 or p.url in linked]
    conn.executemany(
        """
        INSERT INTO pages (url, domain, title, depth, status, hint_score, found_via, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (url) DO UPDATE SET title = excluded.title, depth = MIN(depth, excluded.depth),
            status = excluded.status, hint_score = excluded.hint_score, fetched_at = excluded.fetched_at
        """,
        [(p.url, result.domain, p.title, p.depth, p.status, p.hint_score, p.found_via, stamp) for p in pages],
    )
    conn.executemany(
        """
        INSERT INTO documents (url, domain, title, file_type, found_on, found_via, book_score, first_seen, last_seen)
        VALUES (:url, :domain, :title, :file_type, :found_on, :found_via, :book_score, :stamp, :stamp)
        ON CONFLICT (url) DO UPDATE SET last_seen = excluded.last_seen,
            title = CASE WHEN documents.title = :fallback THEN excluded.title ELSE documents.title END,
            book_score = CASE WHEN documents.title = :fallback THEN excluded.book_score ELSE documents.book_score END
        """,
        [
            {**vars(d), "domain": result.domain, "stamp": stamp, "fallback": title_from_url(d.url),
             "book_score": book_score(d.title, d.url)}
            for d in result.documents.values()
        ],  # fmt: skip
    )
    total_docs = conn.execute("SELECT COUNT(*) FROM documents WHERE domain = ?", (result.domain,)).fetchone()[0]
    conn.execute(
        """
        UPDATE sites SET crawled_at = ?, first_crawled_at = COALESCE(first_crawled_at, ?), crawl_status = ?,
                         home_url = ?, pages_crawled = ?, documents_found = ?, sitemap_urls = ?, error = ?
        WHERE domain = ?
        """,
        (stamp, stamp, result.status, result.home_url, len(result.pages), total_docs, result.sitemap_urls,
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
    books_only: bool = False,
    new_since: str | None = None,
) -> list[sqlite3.Row]:
    """Documents with their site's owner. With new_since, only documents that appeared after that
    time on a site's later crawls; everything on a site's first crawl is the baseline, not news."""
    where, params = ["1 = 1"], []
    for column, value in (("d.domain", domain), ("s.level", level), ("s.state", state and state.upper()),
                          ("d.file_type", file_type)):  # fmt: skip
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if search:
        where.append("(d.title LIKE ? OR d.url LIKE ?)")
        params.extend([f"%{search}%"] * 2)
    if books_only:
        where.append("d.book_score > 0")
    if new_since:
        where.append("d.first_seen > ? AND d.first_seen > s.first_crawled_at")
        params.append(new_since)
    sql = f"""
        SELECT d.*, s.organization, s.suborganization, s.level, s.state
        FROM documents d JOIN sites s ON s.domain = d.domain
        WHERE {' AND '.join(where)}
        ORDER BY d.book_score DESC, s.level, s.organization, d.title
    """
    return conn.execute(sql, params).fetchall()


def seed_urls(conn: sqlite3.Connection, domain: str, limit: int = 50) -> list[str]:
    """Pages worth re-checking first on a re-crawl: where documents were found before, then
    pages that looked like publication listings."""
    rows = conn.execute(
        """
        SELECT url FROM (
            SELECT found_on AS url, 2 AS rank FROM documents WHERE domain = ? AND found_via != 'sitemap'
            UNION
            SELECT url, 1 AS rank FROM pages WHERE domain = ? AND hint_score > 0
        ) WHERE url IS NOT NULL GROUP BY url ORDER BY MAX(rank) DESC, url LIMIT ?
        """,
        (domain, domain, limit),
    ).fetchall()
    return [r["url"] for r in rows]


# --- Amazon checks ------------------------------------------------------------------


def documents_to_check(conn: sqlite3.Connection, older_than: str, limit: int = -1) -> list[sqlite3.Row]:
    """Book-like documents never checked on Amazon, or last checked before ``older_than``; newest first."""
    return conn.execute(
        """
        SELECT d.*, s.organization, s.suborganization
        FROM documents d JOIN sites s ON s.domain = d.domain LEFT JOIN amazon_checks a ON a.url = d.url
        WHERE d.book_score > 0 AND (a.url IS NULL OR a.checked_at < ?)
        ORDER BY d.first_seen DESC, d.book_score DESC LIMIT ?
        """,
        (older_than, limit),
    ).fetchall()


def save_amazon_check(conn: sqlite3.Connection, check: dict) -> None:
    conn.execute(
        """
        INSERT INTO amazon_checks (url, checked_at, provider, query, matching, asins, best_rank, price)
        VALUES (:url, :checked_at, :provider, :query, :matching, :asins, :best_rank, :price)
        ON CONFLICT (url) DO UPDATE SET checked_at = excluded.checked_at, provider = excluded.provider,
            query = excluded.query, matching = excluded.matching, asins = excluded.asins,
            best_rank = excluded.best_rank, price = excluded.price
        """,
        {**check, "asins": json.dumps(check["asins"])},
    )
    conn.commit()


def export_rows(conn: sqlite3.Connection, books_only: bool = True, since: str | None = None,
                new_only: bool = False) -> list[sqlite3.Row]:  # fmt: skip
    """Documents with their site's owner and their latest Amazon check, newest first."""
    where, params = ["1 = 1"], []
    if books_only:
        where.append("d.book_score > 0")
    if since:
        where.append("d.first_seen > ?")
        params.append(since)
    if new_only:
        where.append("d.first_seen > s.first_crawled_at")
    return conn.execute(
        f"""
        SELECT d.*, s.organization, s.suborganization, s.level, s.state, s.first_crawled_at,
               a.checked_at AS amazon_checked_at, a.provider AS amazon_provider, a.matching AS amazon_matching,
               a.asins AS amazon_asins, a.best_rank AS amazon_best_rank, a.query AS amazon_query
        FROM documents d JOIN sites s ON s.domain = d.domain LEFT JOIN amazon_checks a ON a.url = d.url
        WHERE {' AND '.join(where)}
        ORDER BY d.first_seen DESC, d.book_score DESC
        """,
        params,
    ).fetchall()


# --- agencies -------------------------------------------------------------------


def upsert_agency(conn: sqlite3.Connection, agency: dict) -> None:
    conn.execute(
        """
        INSERT INTO agencies (id, name, short_name, level, state, parent_id, website, host, synced_at)
        VALUES (:id, :name, :short_name, :level, :state, :parent_id, :website, :host, :synced_at)
        ON CONFLICT (id) DO UPDATE SET name = excluded.name, short_name = excluded.short_name,
            level = excluded.level, state = excluded.state, parent_id = excluded.parent_id,
            website = excluded.website, host = excluded.host, synced_at = excluded.synced_at
        """,
        {**agency, "synced_at": now()},
    )


def select_agencies(
    conn: sqlite3.Connection, level: str | None = None, state: str | None = None, search: str | None = None
) -> list[sqlite3.Row]:
    where, params = ["1 = 1"], []
    if level:
        where.append("a.level = ?")
        params.append(level)
    if state:
        where.append("a.state = ?")
        params.append(state.upper())
    if search:
        where.append("(a.name LIKE ? OR a.short_name LIKE ? OR a.host LIKE ?)")
        params.extend([f"%{search}%"] * 3)
    return conn.execute(
        f"""
        SELECT a.*, s.crawl_status, s.documents_found, s.crawled_at
        FROM agencies a LEFT JOIN sites s ON s.domain = a.host
        WHERE {' AND '.join(where)} ORDER BY a.level, a.state, a.name
        """,
        params,
    ).fetchall()


# --- watches ----------------------------------------------------------------------


def add_watch(conn: sqlite3.Connection, watch: dict) -> None:
    conn.execute(
        """
        INSERT INTO watches (name, topic, keywords, level, state, domain, search, books_only, created_at)
        VALUES (:name, :topic, :keywords, :level, :state, :domain, :search, :books_only, :created_at)
        ON CONFLICT (name) DO UPDATE SET topic = excluded.topic, keywords = excluded.keywords,
            level = excluded.level, state = excluded.state, domain = excluded.domain, search = excluded.search,
            books_only = excluded.books_only
        """,
        {**watch, "created_at": now()},
    )
    conn.commit()


def list_watches(conn: sqlite3.Connection, names: list[str] | None = None) -> list[sqlite3.Row]:
    if names:
        return conn.execute(
            f"SELECT * FROM watches WHERE name IN ({', '.join('?' for _ in names)}) ORDER BY name", names
        ).fetchall()
    return conn.execute("SELECT * FROM watches ORDER BY name").fetchall()


def remove_watch(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute("DELETE FROM watches WHERE name = ?", (name,))
    conn.commit()
    return cur.rowcount > 0


def mark_watch_run(conn: sqlite3.Connection, name: str, stamp: str) -> None:
    conn.execute("UPDATE watches SET last_run_at = ? WHERE name = ?", (stamp, name))
    conn.commit()


def stats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT level, SUM(parent_domain IS NULL) AS sites, SUM(parent_domain IS NOT NULL) AS subsites,
               SUM(crawled_at IS NOT NULL) AS crawled, SUM(COALESCE(documents_found, 0)) AS documents
        FROM sites WHERE in_registry = 1 GROUP BY level ORDER BY sites DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]
