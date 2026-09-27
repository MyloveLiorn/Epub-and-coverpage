"""Command line interface: `govweb --help`."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

from govweb import db
from govweb.classify import title_from_url
from govweb.crawl import CrawlLimits, crawl_site
from govweb.fetch import USER_AGENT, Fetcher
from govweb.registry import LEVEL_NAMES, REGISTRY_URL, parse_registry
from govweb.tree import build_tree, render, to_dict


def say(message: str = "") -> None:
    print(message, flush=True)


def make_fetcher(args: argparse.Namespace) -> Fetcher:
    return Fetcher(min_interval=args.delay)


def download_registry(url: str) -> str:
    resp = requests.get(url, timeout=60, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.content.decode("utf-8-sig")


def print_table(rows: list[dict], columns: list[tuple[str, str, int]]) -> None:
    if not rows:
        say("(nothing)")
        return

    def fmt(value: object, width: int) -> str:
        text = "" if value is None else str(value)
        return text if len(text) <= width else text[: width - 1] + "…"

    widths = [min(w, max(len(h), *(len(fmt(r.get(k), w)) for r in rows))) for k, h, w in columns]
    say("  ".join(h.ljust(w) for (_, h, _), w in zip(columns, widths)))
    say("  ".join("-" * w for w in widths))
    for row in rows:
        say("  ".join(fmt(row.get(k), cw).ljust(w) for (k, _, cw), w in zip(columns, widths)))


def cmd_sync(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    if args.file:
        text = args.file.read_text(encoding="utf-8-sig")
        say(f"Reading {args.file}...")
    else:
        say(f"Downloading the .gov registry from {args.url}...")
        try:
            text = download_registry(args.url)
        except requests.RequestException as exc:
            say(f"Error: {exc}")
            return 2
    sites = parse_registry(text)
    total, new = db.sync_sites(conn, sites)
    say(f"{total} domains in the registry ({new} new).")
    return cmd_stats(args, conn)


def cmd_stats(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    print_table(db.stats(conn), [("level", "Level", 18), ("sites", "Domains", 8), ("subsites", "Sub-sites", 9),
                                 ("crawled", "Crawled", 8), ("documents", "Documents", 10)])  # fmt: skip
    return 0


def _selected(args: argparse.Namespace, conn: sqlite3.Connection, **extra) -> list[sqlite3.Row]:
    return db.select_sites(
        conn,
        domains=getattr(args, "domains", None),
        level=args.level,
        state=args.state,
        search=args.search,
        include_election=args.include_election,
        **extra,
    )


def cmd_sites(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    sites = _selected(args, conn)
    rows = [dict(s) for s in sites[: args.limit]]
    print_table(rows, [("domain", "Domain", 32), ("level", "Level", 10), ("state", "St", 2),
                       ("organization", "Organization", 45), ("suborganization", "Sub-organization", 30),
                       ("documents_found", "Docs", 5)])  # fmt: skip
    if len(sites) > args.limit:
        say(f"... {len(sites) - args.limit} more (use --limit)")
    return 0


def cmd_map(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    sites = _selected(args, conn)
    if not sites:
        say("No domains. Run: govweb sync")
        return 1
    tree = build_tree(sites)
    if args.out:
        args.out.write_text(json.dumps([to_dict(c) for c in tree.children.values()], indent=2))
        say(f"Wrote the map of {len(sites)} domains to {args.out}")
    else:
        for line in render(tree, max_depth=args.depth):
            say(line)
    return 0


def _clean_host(value: str) -> str:
    """Accept "https://www.usda.gov/path" as well as "usda.gov"."""
    value = value.strip().lower()
    if "://" in value:
        value = urlsplit(value).netloc
    return value.split("/")[0].split(":")[0].removeprefix("www.")


def cmd_crawl(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    args.domains = [_clean_host(d) for d in args.domains]
    if not (args.domains or args.level or args.state or args.search or args.all):
        say("Choose what to crawl: domains, --level, --state, --search, or --all for every domain.")
        return 1
    since = None
    if not args.domains and args.skip_recent_days:
        since = (datetime.now(timezone.utc) - timedelta(days=args.skip_recent_days)).isoformat(timespec="seconds")
    for host in args.domains:  # a subdomain like water.ca.gov becomes a sub-site of ca.gov
        parent = db.registered_domain_of(conn, host)
        if parent and parent["domain"] != host:
            db.add_subsites(conn, parent, [host])
    sites = _selected(args, conn, not_crawled_since=since, limit=args.limit)
    if args.domains:
        missing = set(args.domains) - {s["domain"] for s in sites}
        if missing:
            say(f"Not in the registry (run govweb sync?): {', '.join(sorted(missing))}")
    if not sites:
        say("Nothing to crawl.")
        return 1
    limits = CrawlLimits(
        max_pages=args.max_pages, max_depth=args.max_depth, use_sitemaps=not args.no_sitemaps
    )
    say(f"Crawling {len(sites)} site(s), up to {limits.max_pages} pages each, {args.workers} at a time...")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(crawl_site, s["domain"], make_fetcher(args), limits, s["parent_domain"]): s["domain"]
            for s in sites
        }
        for done, future in enumerate(as_completed(futures), 1):
            domain = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # one broken site must not stop the others
                say(f"[{done}/{len(sites)}] {domain}: crashed ({exc})")
                continue
            new_subsites = db.store_result(conn, result)
            publication_pages = sum(1 for p in result.pages if p.hint_score > 0)
            detail = (
                f"{len(result.pages)} pages ({publication_pages} publication pages), {len(result.documents)} documents"
                + (f", {new_subsites} new sub-sites" if new_subsites else "")
                if result.status == "ok"
                else f"{result.status} {result.error or ''}".strip()
            )
            say(f"[{done}/{len(sites)}] {domain}: {detail}")
    return 0


def _topic_scores(topic_name: str, config_path: Path | None, docs: list[sqlite3.Row]) -> list[tuple[int, sqlite3.Row]]:
    """Score documents with a govbooks topic (title counts 3, words in the file name 1)."""
    from govbooks.config import load_config
    from govbooks.discover import score_topic
    from govbooks.models import Record

    topic = load_config(config_path).topic(topic_name)
    scored = []
    for doc in docs:
        record = Record(source="web", source_id=doc["url"], title=doc["title"], description=title_from_url(doc["url"]))
        score = score_topic(topic, record)
        if score >= topic.min_score:
            scored.append((score, doc))
    return sorted(scored, key=lambda pair: -pair[0])


def cmd_docs(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    docs = db.select_documents(
        conn, domain=args.domain, level=args.level, state=args.state, file_type=args.type, search=args.search
    )
    if args.topic:
        rows = [{**dict(d), "score": score} for score, d in _topic_scores(args.topic, args.config, docs)]
    else:
        rows = [dict(d) for d in docs]
    if args.out:
        columns = ["score", "title", "file_type", "url", "domain", "organization", "suborganization", "level",
                   "state", "found_on", "first_seen"]  # fmt: skip
        with args.out.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        say(f"Wrote {len(rows)} document(s) to {args.out}")
        return 0
    print_table(rows[: args.limit], [("score", "Sc", 3), ("file_type", "Type", 4), ("title", "Title", 60),
                                     ("organization", "Organization", 30), ("url", "URL", 70)])  # fmt: skip
    if len(rows) > args.limit:
        say(f"... {len(rows) - args.limit} more (use --limit or --out)")
    return 0


def cmd_pages(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    sql = "SELECT * FROM pages WHERE hint_score >= ?"
    params: list = [args.min_hint]
    if args.domain:
        sql += " AND domain = ?"
        params.append(args.domain)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY hint_score DESC, url LIMIT ?", [*params, args.limit])]
    print_table(rows, [("hint_score", "Hint", 4), ("title", "Title", 50), ("url", "URL", 80)])
    return 0


def _site_filters(p: argparse.ArgumentParser) -> None:
    p.add_argument("--level", choices=LEVEL_NAMES, help="level of government")
    p.add_argument("--state", help="two-letter state code, e.g. CA")
    p.add_argument("--search", help="organization or domain contains this text")
    p.add_argument("--include-election", action="store_true", help="include election-office domains")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="govweb",
        description="Map every US government .gov website and find the publications they host.",
    )
    parser.add_argument("--db", type=Path, default=Path("govweb.db"), help="database file (default: govweb.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="download the official .gov domain registry (CISA)")
    p.add_argument("--url", default=REGISTRY_URL)
    p.add_argument("--file", type=Path, help="read a local copy of the registry CSV instead")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("stats", help="domains, crawled sites and documents per level")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("sites", help="list domains")
    _site_filters(p)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_sites)

    p = sub.add_parser("map", help="the domains as a tree: level > state or organization > domain")
    _site_filters(p)
    p.add_argument("--depth", type=int, help="stop the tree at this depth (0 = levels only)")
    p.add_argument("--out", type=Path, help="write the tree as JSON instead of printing it")
    p.set_defaults(func=cmd_map)

    p = sub.add_parser("crawl", help="crawl sites for publication pages and documents")
    p.add_argument("domains", nargs="*", help="specific domains, e.g. usda.gov")
    _site_filters(p)
    p.add_argument("--all", action="store_true", help="every domain matching the filters (or the whole registry)")
    p.add_argument("--limit", type=int, help="at most this many sites")
    p.add_argument("--max-pages", type=int, default=100, help="pages fetched per site (default 100)")
    p.add_argument("--max-depth", type=int, default=3, help="link depth from the home page (default 3)")
    p.add_argument("--no-sitemaps", action="store_true", help="don't read sitemaps")
    p.add_argument("--workers", type=int, default=4, help="sites crawled in parallel (each site one at a time)")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests to one host (default 1)")
    p.add_argument("--skip-recent-days", type=int, default=30, help="skip sites crawled in the last N days")
    p.set_defaults(func=cmd_crawl)

    p = sub.add_parser("docs", help="documents found by crawling")
    p.add_argument("--domain")
    p.add_argument("--level", choices=LEVEL_NAMES)
    p.add_argument("--state")
    p.add_argument("--type", help="pdf, epub, mobi, docx, ...")
    p.add_argument("--search", help="title or URL contains this text")
    p.add_argument("--topic", help="a topic from govbooks.toml (keeps and ranks matching documents)")
    p.add_argument("--config", type=Path, help="govbooks config for --topic (default: ./govbooks.toml)")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--out", type=Path, help="write all matches to a CSV file")
    p.set_defaults(func=cmd_docs)

    p = sub.add_parser("pages", help="publication listing pages found by crawling")
    p.add_argument("--domain")
    p.add_argument("--min-hint", type=int, default=1)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_pages)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "state", None):
        args.state = args.state.upper()
    conn = db.connect(args.db)
    try:
        return args.func(args, conn) or 0
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:  # output piped into `head` and closed early
        sys.stderr.close()
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
