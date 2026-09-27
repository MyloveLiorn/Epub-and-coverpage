"""Command line interface: `govbooks --help`."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from govbooks import agencies as agency_map
from govbooks import copyright_policy, db
from govbooks.config import Config, load_config
from govbooks.discover import discover
from govbooks.http import Http, HttpError
from govbooks.market import PROVIDERS, VERDICT_HELP, ProviderError, build_provider, check_books, topic_demand
from govbooks.report import write_csv, write_markdown
from govbooks.sources import build_sources
from govbooks.states import resolve_state
from govbooks.tracking import run_tracking

EXAMPLE_CONFIG = Path(__file__).resolve().parent / "example.toml"
RIGHTS_OK = copyright_policy.REUSABLE


def say(message: str = "") -> None:
    print(message, flush=True)


def make_http() -> Http:
    return Http()


def print_table(rows: list[dict], columns: list[tuple[str, str, int]]) -> None:
    """columns: (key, header, max width)."""
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


# --- init -------------------------------------------------------------------


def cmd_init(args: argparse.Namespace, config: Config) -> int:
    target = args.config or Path("govbooks.toml")
    if target.exists() and not args.force:
        say(f"{target} already exists (use --force to overwrite).")
        return 1
    shutil.copyfile(EXAMPLE_CONFIG, target)
    say(f"Wrote {target}. Edit the [topics] section, then run: govbooks agencies sync")
    return 0


# --- agencies ---------------------------------------------------------------


def _states_arg(values: list[str] | None, config: Config) -> list[str] | None:
    raw = values or config.states
    return [resolve_state(v) for v in raw] if raw else None


def cmd_agencies_sync(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    http = make_http()
    total = 0
    if not args.states_only:
        say("Federal agencies (Federal Register)...")
        federal = agency_map.fetch_federal_agencies(http)
        total += db.upsert_agencies(conn, federal)
        say(f"  {len(federal) - 1} federal agencies")
    if not args.federal_only:
        say("State agencies (Wikidata)...")
        states = agency_map.fetch_state_agencies(http, _states_arg(args.state, config), progress=say)
        total += db.upsert_agencies(conn, states)
    say(f"Saved {total} entries to {config.database}.")
    return 0


def _filter_agencies(args: argparse.Namespace, conn: sqlite3.Connection) -> list:
    agencies = db.all_agencies(conn)
    if getattr(args, "level", None):
        agencies = [a for a in agencies if a.level == args.level]
    if getattr(args, "state", None):
        states = {resolve_state(s) for s in args.state}
        agencies = [a for a in agencies if a.jurisdiction in states]
    if getattr(args, "search", None):
        needle = args.search.lower()
        agencies = [a for a in agencies if needle in a.name.lower() or needle in (a.short_name or "").lower()]
    return agencies


def cmd_agencies_list(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    agencies = sorted(_filter_agencies(args, conn), key=lambda a: (a.level != "federal", a.jurisdiction, a.name))
    rows = [asdict(a) for a in agencies[: args.limit]]
    print_table(rows, [("id", "ID", 16), ("short_name", "Short", 10), ("name", "Name", 60), ("jurisdiction", "Where", 16)])
    if len(agencies) > args.limit:
        say(f"... {len(agencies) - args.limit} more (use --limit)")
    return 0


def cmd_agencies_tree(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    agencies = _filter_agencies(args, conn)
    if not agencies:
        say("No agencies stored yet. Run: govbooks agencies sync")
        return 1
    for line in agency_map.render_tree(agencies, root_id=args.root, max_depth=args.depth):
        say(line)
    return 0


def cmd_agencies_export(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    agencies = _filter_agencies(args, conn)
    out: Path = args.out
    if out.suffix == ".csv":
        with out.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(asdict(agencies[0]).keys()) if agencies else ["id"])
            writer.writeheader()
            writer.writerows(asdict(a) for a in agencies)
    else:
        out.write_text(json.dumps(agency_map.tree_dict(agencies), indent=2))
    say(f"Wrote {len(agencies)} agencies to {out}")
    return 0


# --- discover and books -----------------------------------------------------------


def _select_agencies(args: argparse.Namespace, conn: sqlite3.Connection) -> list | None:
    """The agencies to search one by one, or None to search government publishers in general."""
    if not (args.agency or args.under or args.state):
        return None
    everything = db.all_agencies(conn)
    chosen = {}
    by_id = {a.id: a for a in everything}
    for agency_id in [*(args.agency or []), *(args.under or [])]:
        if agency_id not in by_id:
            raise SystemExit(f"Unknown agency id {agency_id!r}. Find ids with: govbooks agencies list --search ...")
    for agency_id in args.agency or []:
        chosen[agency_id] = by_id[agency_id]
    for root in args.under or []:
        for agency_id in agency_map.subtree_ids(everything, root):
            chosen[agency_id] = by_id[agency_id]
    for state in args.state or []:
        name = resolve_state(state)
        chosen.update({a.id: a for a in everything if a.jurisdiction == name and a.level == "state"})
    # Synthetic roots ("State of Ohio") are not publishers.
    selected = [a for a in chosen.values() if a.source != "govbooks"]
    if not selected:
        raise SystemExit("No agencies matched. For states, map them first: govbooks agencies sync --state XX")
    if len(selected) > args.max_agencies:
        say(f"{len(selected)} agencies selected; searching the first {args.max_agencies} (raise --max-agencies).")
        selected = sorted(selected, key=lambda a: a.name)[: args.max_agencies]
    return selected


def cmd_discover(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    topic = config.topic(args.topic)
    agencies = db.all_agencies(conn)
    if not agencies:
        say("Note: the agency map is empty, so books are matched by publisher only. Run: govbooks agencies sync")
    selected = _select_agencies(args, conn)
    http = make_http()
    sources = build_sources(args.source or config.sources, http, config)
    say(f"Searching {len(sources)} source(s) for topic {topic.name!r} ({len(topic.keywords)} keyword(s))...")
    stats = discover(
        conn,
        topic,
        sources,
        agency_map.AgencyIndex(agencies),
        agencies=selected,
        limit=args.limit or config.max_results,
        progress=say,
    )
    say(
        f"{stats.queries} queries, {stats.records} results, {stats.kept} relevant government records, "
        f"{stats.new_books} new book(s)."
    )
    if stats.errors:
        say(f"{len(stats.errors)} query(ies) failed; see messages above.")
    return 0


BOOK_COLUMNS = [
    ("id", "ID", 16),
    ("market_score", "Mkt", 4),
    ("verdict", "Verdict", 16),
    ("topic_score", "Topic", 5),
    ("year", "Year", 4),
    ("rights", "Rights", 20),
    ("title", "Title", 60),
    ("agency", "Agency", 30),
]


def _book_rows(books: list[sqlite3.Row]) -> list[dict]:
    return [{**dict(b), "agency": b["agency_name"] or (b["level"] or "").title()} for b in books]


def cmd_books_list(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    books = db.list_books(
        conn, topic=args.topic, min_score=args.min_score, rights=args.rights, level=args.level, limit=args.limit
    )
    print_table(_book_rows(books), BOOK_COLUMNS)
    return 0


def cmd_books_show(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    book = db.get_book(conn, args.id)
    if not book:
        say(f"No book with id {args.id}")
        return 1
    agency = db.get_agency(conn, book["agency_id"]) if book["agency_id"] else None
    say(book["title"] + (f": {book['subtitle']}" if book["subtitle"] else ""))
    say(f"  year: {book['year']}   level: {book['level']}   agency: {agency.name if agency else '-'}")
    say(f"  authors: {'; '.join(db.loads(book['authors'])) or '-'}")
    say(f"  publisher: {book['publisher'] or '-'}")
    say(f"  rights: {book['rights']} ({book['rights_note']})")
    say(f"  full text: {book['fulltext_url'] or '-'}")
    say(f"  ISBNs: {', '.join(db.loads(book['isbns'])) or '-'}")
    topics = conn.execute("SELECT topic, score FROM book_topics WHERE book_id = ?", (args.id,)).fetchall()
    say("  topics: " + ", ".join(f"{t['topic']} ({t['score']})" for t in topics))
    for source in db.loads(book["sources"]):
        say(f"  source: {source['source']}  {source.get('url') or source['id']}")
    checks = conn.execute(
        "SELECT * FROM market_checks WHERE book_id = ? ORDER BY checked_at DESC LIMIT 3", (args.id,)
    ).fetchall()
    for check in checks:
        say(
            f"  market ({check['provider']}, {check['checked_at']}): {check['verdict']}, score {check['score']}, "
            f"{check['matching_listings']} listing(s), best rank {check['best_rank'] or '-'}"
        )
        for listing in db.loads(check["listings"]):
            say(f"    - {listing['asin']}  {listing['title'][:70]}  {listing.get('url') or ''}")
    return 0


# --- market -----------------------------------------------------------------


def cmd_market_check(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    provider = build_provider(args.provider or config.amazon_provider, make_http(), config)
    rights = None if args.any_rights else RIGHTS_OK
    books = db.list_books(conn, topic=args.topic, min_score=args.min_score, rights=rights, limit=args.limit)
    if not books:
        say("No matching books. Run discover first, or loosen --min-score / --any-rights.")
        return 1
    topic_rank = None
    if args.topic:
        try:
            topic_rank = topic_demand(provider, config.topic(args.topic).keywords)
        except (HttpError, ProviderError, OSError, ValueError) as exc:
            say(f"Topic demand check failed ({exc}); continuing without it.")
        if topic_rank:
            say(f"Topic demand: median best-seller rank {topic_rank:,}")
    say(f"Checking {len(books)} book(s) with {provider.name}...")
    results = check_books(
        conn, provider, books, topic_rank, skip_checked_within_days=0 if args.recheck else 7, progress=say
    )
    say(f"Checked {len(results)} book(s) (books checked in the last 7 days are skipped unless --recheck).")
    if args.watch_top:
        best = sorted(results, key=lambda pair: pair[1].score, reverse=True)[: args.watch_top]
        for book, _ in best:
            db.add_watch(conn, label=book["title"][:80], book_id=book["id"])
        say(f"Added {len(best)} book(s) to the watchlist.")
    return 0


def cmd_market_verdicts(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    for name, text in VERDICT_HELP.items():
        say(f"{name:17} {text}")
    return 0


# --- tracking ---------------------------------------------------------------


def cmd_track_add(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    if args.book:
        book = db.get_book(conn, args.book)
        if not book:
            say(f"No book with id {args.book}")
            return 1
        watch_id = db.add_watch(conn, label=args.label or book["title"][:80], book_id=args.book)
    else:
        watch_id = db.add_watch(conn, label=args.label or args.asin, asin=args.asin.strip().upper())
    say(f"Watching (watch id {watch_id}).")
    return 0


def cmd_track_remove(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    ok = db.deactivate_watch(conn, args.watch_id)
    say("Stopped watching." if ok else f"No watch with id {args.watch_id}")
    return 0 if ok else 1


def cmd_track_list(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    rows = []
    for watch in db.list_watches(conn):
        history = db.snapshots_for(conn, watch["id"])
        last = history[-1] if history else None
        rows.append(
            {
                "id": watch["id"],
                "what": f"ASIN {watch['asin']}" if watch["asin"] else f"book {watch['book_id']}",
                "label": watch["label"],
                "rank": f"{last['sales_rank']:,}" if last and last["sales_rank"] else "",
                "price": f"${last['price']:.2f}" if last and last["price"] else "",
                "listings": last["listing_count"] if last else "",
                "checked": last["taken_at"] if last else "never",
            }
        )
    print_table(
        rows,
        [("id", "ID", 4), ("what", "Watching", 22), ("label", "Label", 50), ("rank", "Rank", 10),
         ("price", "Price", 8), ("listings", "Listings", 8), ("checked", "Last checked", 25)],
    )  # fmt: skip
    return 0


def cmd_track_run(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    if not db.list_watches(conn):
        say("The watchlist is empty. Add books with: govbooks track add --book ID")
        return 1
    provider = build_provider(args.provider or config.amazon_provider, make_http(), config)
    changes = run_tracking(conn, provider)
    for change in changes:
        say(f"[{change.watch_id}] {change.label[:60]}: {change.message}")
    if not changes:
        say("No changes since the last run.")
    return 0


def cmd_track_history(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    rows = [dict(s) for s in db.snapshots_for(conn, args.watch_id)]
    print_table(
        rows,
        [("taken_at", "When", 25), ("provider", "Provider", 9), ("sales_rank", "Rank", 10), ("price", "Price", 8),
         ("listing_count", "Listings", 8), ("title", "Title", 50)],
    )  # fmt: skip
    return 0


# --- report and run ---------------------------------------------------------------


def cmd_report(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    rights = None if args.any_rights else RIGHTS_OK
    books = db.list_books(conn, topic=args.topic, min_score=args.min_score, rights=rights, limit=args.limit)
    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix == ".csv":
        write_csv(books, out, config.marketplace)
    else:
        title = f"Government books: {args.topic}" if args.topic else "Government books"
        write_markdown(books, out, title, config.marketplace)
    say(f"Wrote {len(books)} book(s) to {out}")
    return 0


def cmd_run(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    """The whole pipeline for one topic: map, discover, check, report."""
    if not db.all_agencies(conn):
        sync_args = argparse.Namespace(states_only=False, federal_only=args.federal_only, state=None)
        cmd_agencies_sync(sync_args, config, conn)
    discover_args = argparse.Namespace(
        topic=args.topic, source=None, limit=args.limit, agency=None, under=None, state=None, max_agencies=0
    )
    cmd_discover(discover_args, config, conn)
    market_args = argparse.Namespace(
        provider=args.provider, topic=args.topic, min_score=0, any_rights=False, limit=args.check_limit,
        recheck=False, watch_top=args.watch_top,
    )  # fmt: skip
    cmd_market_check(market_args, config, conn)
    out = args.out or Path("reports") / f"{args.topic}.md"
    report_args = argparse.Namespace(topic=args.topic, min_score=0, any_rights=False, limit=None, out=out)
    return cmd_report(report_args, config, conn)


# --- parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="govbooks",
        description="Map US federal and state government offices, find the books they publish on your "
        "topics, and check and track those books on Amazon.",
    )
    parser.add_argument("--config", type=Path, help="config file (default: ./govbooks.toml)")
    parser.add_argument("--db", type=Path, help="database file (overrides the config)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write an example govbooks.toml")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init, needs_db=False)

    agencies = sub.add_parser("agencies", help="the map of government offices").add_subparsers(
        dest="agencies_command", required=True
    )
    p = agencies.add_parser("sync", help="download federal (Federal Register) and state (Wikidata) agencies")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--federal-only", action="store_true")
    group.add_argument("--states-only", action="store_true")
    p.add_argument("--state", action="append", help="only these states (repeatable, e.g. --state CA)")
    p.set_defaults(func=cmd_agencies_sync)

    for name, func, help_text in (
        ("list", cmd_agencies_list, "list agencies"),
        ("tree", cmd_agencies_tree, "print the agency hierarchy"),
        ("export", cmd_agencies_export, "export the map as nested JSON or flat CSV"),
    ):
        p = agencies.add_parser(name, help=help_text)
        p.add_argument("--level", choices=["federal", "state"])
        p.add_argument("--state", action="append", help="only agencies of these states (repeatable)")
        p.add_argument("--search", help="name contains this text")
        if name == "list":
            p.add_argument("--limit", type=int, default=100)
        if name == "tree":
            p.add_argument("--root", help="start from this agency id")
            p.add_argument("--depth", type=int, help="maximum depth")
        if name == "export":
            p.add_argument("--out", type=Path, required=True, help="output file (.json or .csv)")
        p.set_defaults(func=func)

    p = sub.add_parser("discover", help="search the sources for books on a topic")
    p.add_argument("topic", help="topic name from the config")
    p.add_argument("--source", action="append", help="only these sources (repeatable)")
    p.add_argument("--limit", type=int, help="max results per query")
    p.add_argument("--agency", action="append", help="search by this agency as author (repeatable)")
    p.add_argument("--under", action="append", help="search by every agency under this agency id")
    p.add_argument("--state", action="append", help="search by every agency of this state")
    p.add_argument("--max-agencies", type=int, default=25, help="cap on agencies searched one by one")
    p.set_defaults(func=cmd_discover)

    books = sub.add_parser("books", help="discovered books").add_subparsers(dest="books_command", required=True)
    p = books.add_parser("list", help="list books, best first")
    p.add_argument("--topic")
    p.add_argument("--min-score", type=int, default=0, help="minimum topic score")
    p.add_argument("--rights", action="append", choices=copyright_policy.RIGHTS_VALUES)
    p.add_argument("--level", choices=["federal", "state"])
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_books_list)
    p = books.add_parser("show", help="everything known about one book")
    p.add_argument("id")
    p.set_defaults(func=cmd_books_show)

    market = sub.add_parser("market", help="Amazon checks").add_subparsers(dest="market_command", required=True)
    p = market.add_parser("check", help="check discovered books against Amazon")
    p.add_argument("--provider", choices=PROVIDERS, help="default: [amazon] provider in the config")
    p.add_argument("--topic")
    p.add_argument("--min-score", type=int, default=0, help="minimum topic score")
    p.add_argument("--any-rights", action="store_true", help="include books whose copyright status is unclear")
    p.add_argument("--limit", type=int, default=50, help="max books to check")
    p.add_argument("--recheck", action="store_true", help="also re-check books checked in the last 7 days")
    p.add_argument("--watch-top", type=int, default=0, help="add the N best-scoring books to the watchlist")
    p.set_defaults(func=cmd_market_check)
    p = market.add_parser("verdicts", help="explain the verdicts")
    p.set_defaults(func=cmd_market_verdicts)

    track = sub.add_parser("track", help="watch books and ASINs over time").add_subparsers(
        dest="track_command", required=True
    )
    p = track.add_parser("add", help="watch a book (for new competing editions) or an ASIN (for rank and price)")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--book", help="book id")
    target.add_argument("--asin", help="Amazon ASIN")
    p.add_argument("--label")
    p.set_defaults(func=cmd_track_add)
    p = track.add_parser("remove", help="stop watching")
    p.add_argument("watch_id", type=int)
    p.set_defaults(func=cmd_track_remove)
    p = track.add_parser("list", help="the watchlist with the latest numbers")
    p.set_defaults(func=cmd_track_list)
    p = track.add_parser("run", help="take a snapshot of everything watched and report changes")
    p.add_argument("--provider", choices=PROVIDERS)
    p.set_defaults(func=cmd_track_run)
    p = track.add_parser("history", help="all snapshots of one watch")
    p.add_argument("watch_id", type=int)
    p.set_defaults(func=cmd_track_history)

    p = sub.add_parser("report", help="export books as Markdown or CSV")
    p.add_argument("--out", type=Path, required=True, help="output file (.md or .csv)")
    p.add_argument("--topic")
    p.add_argument("--min-score", type=int, default=0)
    p.add_argument("--any-rights", action="store_true", help="include books whose copyright status is unclear")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("run", help="full pipeline for a topic: map, discover, market check, report")
    p.add_argument("topic")
    p.add_argument("--provider", choices=PROVIDERS)
    p.add_argument("--federal-only", action="store_true", help="skip state agencies when building the map")
    p.add_argument("--limit", type=int, help="max results per source query")
    p.add_argument("--check-limit", type=int, default=50, help="max books to check on Amazon")
    p.add_argument("--watch-top", type=int, default=0, help="add the N best books to the watchlist")
    p.add_argument("--out", type=Path, help="report file (default: reports/<topic>.md)")
    p.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    if args.db:
        config.database = args.db
    if not getattr(args, "needs_db", True):
        return args.func(args, config)
    conn = db.connect(config.database)
    try:
        return args.func(args, config, conn) or 0
    except (HttpError, ProviderError) as exc:
        say(f"Error: {exc}")
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
