"""Command line interface: `govweb --help`."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from operator import itemgetter
from pathlib import Path
from urllib.parse import urlsplit

import requests

from govbooks import copyright_policy
from govbooks.http import Http, HttpError
from govbooks.states import STATE_ABBR, resolve_state
from govweb import db
from govweb.agencies import (
    fetch_directory,
    guess_owner,
    import_agencies,
    remove_non_government_sites,
)
from govweb.amazon import check_documents
from govweb.classify import host_hint_score, topic_phrases
from govweb.crawl import CrawlLimits, crawl_site
from govweb.exclude import excluded_states, is_excluded, is_excluded_state_site
from govweb.export import (
    book_order,
    dedupe,
    pick_version,
    result_rows,
    variant_groups,
    write_csv,
    write_markdown,
    year_order,
)
from govweb.fetch import USER_AGENT, Fetcher
from govweb.portals import ensure_portal, is_portal, state_portals
from govweb.registry import LEVEL_NAMES, REGISTRY_URL, parse_registry
from govweb.tree import build_tree, render, to_dict
from govweb.watch import (
    keyword_list,
    make_matcher,
    markdown_report,
    matching_topics,
    rank,
    topic_keywords,
    topic_matchers,
)


def say(message: str = "") -> None:
    print(message, flush=True)


def make_fetcher(args: argparse.Namespace) -> Fetcher:
    return Fetcher(min_interval=args.delay)


def make_json_http() -> Http:
    return Http()


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


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
    removed = remove_non_government_sites(conn)
    if removed:
        say(f"Removed {removed} agency-directory site(s) that are not on a government domain.")
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


def _ensure_sites(conn: sqlite3.Connection, hosts: list[str]) -> None:
    """Make every host crawlable: subdomains join their registry domain, anything else is added
    as a hand-added site, so any website can be searched."""
    for host in hosts:
        if db.get_site(conn, host):
            continue
        parent = db.registered_domain_of(conn, host)
        if parent:
            db.add_subsites(conn, parent, [host])
            continue
        level, state = guess_owner(host)
        db.add_site(conn, host, organization=host, level=level, state=state)
        known = "a known government site" if level == "other" else f"on the map yet ({level} by its domain)"
        say(f"{host} is not {known}; added it (see: govweb add --help to name it).")


def run_crawls(conn: sqlite3.Connection, sites: list[sqlite3.Row], args: argparse.Namespace, aliases: bool = True) -> int:
    """Crawl sites in parallel and store the results. Re-crawls start from known listing pages.
    With ``aliases``, a site that only redirects to another mapped site is not crawled twice."""
    limits = CrawlLimits(
        max_pages=args.max_pages, max_depth=args.max_depth, use_sitemaps=not args.no_sitemaps,
        topic_keywords=getattr(args, "crawl_keywords", None) or [],
        max_seconds=args.max_minutes * 60 if args.max_minutes else None,
    )  # fmt: skip
    say(f"Crawling {len(sites)} site(s), up to {limits.max_pages} pages each, {args.workers} at a time...")
    # A site that redirects to one of these is an alias; one that redirects to a site that
    # couldn't be reached directly is still crawled, through the alias.
    mapped = {r[0] for r in conn.execute("SELECT domain FROM sites WHERE crawl_status IS NULL OR crawl_status IN "
                                         "('ok', 'alias')")}  # fmt: skip
    new_documents = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                crawl_site, s["domain"], make_fetcher(args), limits, s["parent_domain"],
                db.seed_urls(conn, s["domain"]), mapped.__contains__ if aliases else None,
            ): s
            for s in sites
        }  # fmt: skip
        for done, future in enumerate(as_completed(futures), 1):
            site = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # one broken site must not stop the others
                say(f"[{done}/{len(sites)}] {site['domain']}: crashed ({exc})")
                continue
            known = {r[0] for r in conn.execute("SELECT url FROM documents WHERE domain = ?", (site["domain"],))}
            fresh = len(set(result.documents) - known)
            new_subsites = db.store_result(conn, result)
            if site["crawled_at"]:  # the first crawl is the baseline, not news
                new_documents += fresh
            publication_pages = sum(1 for p in result.pages if p.hint_score > 0)
            detail = (
                f"{len(result.pages)} pages ({publication_pages} publication pages), {len(result.documents)} documents"
                + (f" ({fresh} new)" if site["crawled_at"] and fresh else "")
                + (f", {new_subsites} new sub-sites" if new_subsites else "")
                + (f"; {result.error}" if result.error else "")
                if result.status == "ok"
                else f"same website as {urlsplit(result.home_url).netloc}, not crawled twice"
                if result.status == "alias"
                else f"{result.status} {result.error or ''}".strip()
            )
            say(f"[{done}/{len(sites)}] {site['domain']}: {detail}")
    return new_documents


def cmd_crawl(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    args.domains = [_clean_host(d) for d in args.domains]
    if not (args.domains or args.level or args.state or args.search or args.all):
        say("Choose what to crawl: domains, --level, --state, --search, or --all for every domain.")
        return 1
    recent = _since(args.skip_recent_days) if args.skip_recent_days else None
    _ensure_sites(conn, args.domains)
    args.crawl_keywords = _crawl_keywords(args)
    sites = [s for s in _selected(args, conn, not_crawled_since=None if args.domains else recent, rotation=True)
             if not is_excluded(s["domain"]) and not is_excluded_state_site(s["level"], s["state"])]  # fmt: skip
    for domain in args.domains:
        if is_excluded(domain):
            say(f"{domain} is on the list of sites never searched (govweb/data/excluded.txt).")
    if args.state and args.state.upper() in excluded_states():
        say(f"{args.state.upper()}'s own sites are never searched: its publications can't be reused "
            "(govweb/data/excluded_states.txt).")
    sites = _by_promise(sites, args.crawl_keywords, keyword_list(args.prefer_names))[: args.limit]
    if not sites:  # not an error: everything chosen was crawled recently (--skip-recent-days)
        say("Nothing to crawl: every chosen site was crawled recently.")
        return 0
    if args.crawl_keywords:
        say(f"Following links about: {', '.join(args.crawl_keywords)}")
    if not args.subsites_only:
        run_crawls(conn, sites, args, aliases=not args.domains)  # sites asked for by name are always crawled
    if args.with_subsites or args.subsites_only:
        subsites = _subsites_to_crawl(conn, [s["domain"] for s in sites], recent, args)
        if subsites:
            say(f"\nSub-sites: {', '.join(s['domain'] for s in subsites)}")
            sub_args = argparse.Namespace(**{**vars(args), "max_pages": args.subsite_pages or args.max_pages,
                                             "max_minutes": args.subsite_minutes or args.max_minutes})
            run_crawls(conn, subsites, sub_args)
        else:
            say("No sub-sites due for a crawl.")
    return 0


def _crawl_keywords(args: argparse.Namespace) -> list[str]:
    """Keywords whose links a crawl follows first: --topic / --all-topics from govbooks.toml, and --keywords."""
    names = args.topic or []
    keywords = topic_keywords(args.config, names) if names or args.all_topics else []
    return list(dict.fromkeys(keywords + keyword_list(args.keywords)))


def _by_promise(sites: list[sqlite3.Row], keywords: list[str], prefer: list[str] | None = None) -> list[sqlite3.Row]:
    """Never-crawled sites first, those whose names suggest publishers (armypubs.army.mil,
    alabamaarchives.gov) or the searched topics leading; then the sites crawled longest ago.
    With ``prefer`` (parts of site names: "librar", "refugee"; "-court" for names to put last),
    the state's portal and the sites so named come first, whether crawled before or not."""
    phrases = topic_phrases(keywords)
    if prefer:
        def named(site: sqlite3.Row) -> int:
            # A sub-site by its own name: app3.azdhs.gov isn't a human-services site for its parent's "dhs".
            host = site["domain"]
            parent = site["parent_domain"] if "parent_domain" in site.keys() else None
            if parent and host.endswith("." + parent):
                host = host[: -len(parent) - 1] + ".x"
            host = host.replace("-", "")
            score = host_hint_score(host, phrases)
            for part in prefer:
                word = part.lower().replace(" ", "").lstrip("-")
                if word and word in host:
                    score += -3 if part.startswith("-") else 3
            return score

        return sorted(sites, key=lambda s: (not is_portal(s["domain"]), -named(s), s["crawled_at"] is not None))

    def key(site: sqlite3.Row) -> tuple:
        if site["crawled_at"]:
            return (1, not is_portal(site["domain"]), site["crawled_at"])
        return (0, not is_portal(site["domain"]), -host_hint_score(site["domain"], phrases), "")

    return sorted(sites, key=key)


def _subsites_to_crawl(
    conn: sqlite3.Connection, parents: list[str], recent: str | None, args: argparse.Namespace
) -> list[sqlite3.Row]:
    """The sub-sites of the crawled sites that are due (not those just crawled), the most promising first."""
    subsites = [s for s in db.select_subsites(conn, parents, not_crawled_since=recent)
                if s["domain"] not in parents and not is_excluded(s["domain"])
                and not is_excluded_state_site(s["level"], s["state"])]  # fmt: skip
    return _by_promise(subsites, args.crawl_keywords, keyword_list(getattr(args, "prefer_names", None)))[
        : args.max_subsites
    ]


def cmd_portals(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    """With states: put their portals on the map and print them (one per line). Without: list all."""
    if args.states:
        for state in args.states:
            say(ensure_portal(conn, STATE_ABBR[resolve_state(state)]))
        return 0
    rows = []
    for abbr, host in sorted(state_portals().items()):
        site = db.get_site(conn, host)
        rows.append({"state": abbr, "portal": host, "status": (site["crawl_status"] or "not crawled") if site else "not on the map",
                     "documents": site["documents_found"] if site else ""})  # fmt: skip
    print_table(rows, [("state", "St", 2), ("portal", "Portal", 24), ("status", "Status", 14), ("documents", "Docs", 6)])
    return 0


def cmd_ntrs(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    from govweb import ntrs

    _ensure_sites(conn, ["ntrs.nasa.gov"])
    try:
        citations = ntrs.search(make_json_http(), args.query, limit=args.limit)
    except HttpError as exc:
        say(f"Error: {exc}")
        return 2
    result = ntrs.to_result(citations)
    known = {r[0] for r in conn.execute("SELECT url FROM documents WHERE domain = 'ntrs.nasa.gov'")}
    db.store_result(conn, result)
    say(f"NASA Technical Reports Server: {len(citations)} report(s) for {args.query!r}, {len(result.documents)} "
        f"with a public file ({len(set(result.documents) - known)} not seen before).")  # fmt: skip
    return 0


def cmd_add(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    host = _clean_host(args.url)
    abbr = STATE_ABBR[resolve_state(args.state)] if args.state else None
    if db.get_site(conn, host):
        conn.execute(
            "UPDATE sites SET organization = COALESCE(?, organization), level = COALESCE(?, level), "
            "state = COALESCE(?, state) WHERE domain = ?",
            (args.name, args.level, abbr, host),
        )
        conn.commit()
        say(f"Updated {host}.")
    else:
        db.add_site(conn, host, organization=args.name or host, level=args.level or "other", state=abbr)
        say(f"Added {host}. Crawl it with: govweb crawl {host}")
    return 0


DOC_COLUMNS = ["score", "book_score", "rights", "title", "file_type", "url", "domain", "organization",
               "suborganization", "level", "state", "found_on", "first_seen", "rights_note"]  # fmt: skip


def _show_documents(rows: list[dict], args: argparse.Namespace) -> None:
    if getattr(args, "out", None):
        with args.out.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=DOC_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        say(f"Wrote {len(rows)} document(s) to {args.out}")
        return
    print_table(rows[: args.limit], [("score", "Sc", 3), ("rights", "Rights", 20), ("file_type", "Type", 4),
                                     ("title", "Title", 55), ("organization", "Organization", 28),
                                     ("url", "URL", 70)])  # fmt: skip
    if len(rows) > args.limit:
        say(f"... {len(rows) - args.limit} more (use --limit or --out)")


def _matched_rows(args: argparse.Namespace, docs: list[sqlite3.Row]) -> list[dict]:
    matcher = make_matcher(args.topic, keyword_list(args.keywords), args.config)
    return [
        {**dict(f.doc), "score": f.score, "rights": f.rights.status, "rights_note": f.rights.note}
        for f in rank(docs, matcher, reusable_only=args.reusable)
    ]


def cmd_docs(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    docs = db.select_documents(
        conn, domain=args.domain, level=args.level, state=args.state, file_type=args.type, search=args.search,
        books_only=args.books_only,
    )  # fmt: skip
    _show_documents(_matched_rows(args, docs), args)
    return 0


def cmd_new(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    """Documents that appeared on re-crawls in the last N days."""
    docs = db.select_documents(
        conn, domain=args.domain, level=args.level, state=args.state, file_type=args.type, search=args.search,
        books_only=args.books_only, new_since=args.since or _since(args.days),
    )  # fmt: skip
    rows = _matched_rows(args, docs)
    say(f"{len(rows)} new document(s) since {args.since or f'{args.days} day(s) ago'}.")
    _show_documents(rows, args)
    return 0


# --- agencies ---------------------------------------------------------------------


def cmd_agencies_sync(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    states = [resolve_state(s) for s in args.state] if args.state else None
    try:
        agencies = fetch_directory(
            make_json_http(), federal=not args.states_only, states=[] if args.federal_only else states, progress=say
        )
    except HttpError as exc:
        say(f"Error: {exc}")
        return 2
    stats = import_agencies(conn, agencies)
    say(
        f"{stats.agencies} agencies, {stats.with_website} with a website: {stats.new_sites} new sites outside the "
        f".gov registry, {stats.new_subsites} new sub-sites, {stats.named_subsites + stats.named_sites} sites named, "
        f"{stats.not_government} skipped as not on a government domain"
        + (f", {stats.removed_sites} earlier non-government sites removed." if stats.removed_sites else ".")
    )
    return 0


def cmd_agencies_list(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    rows = [dict(r) for r in db.select_agencies(conn, level=args.level, state=args.state, search=args.search)]
    print_table(rows[: args.limit], [("id", "ID", 14), ("level", "Level", 7), ("state", "St", 2), ("name", "Agency", 50),
                                     ("host", "Website", 30), ("documents_found", "Docs", 5)])  # fmt: skip
    if len(rows) > args.limit:
        say(f"... {len(rows) - args.limit} more (use --limit)")
    return 0


# --- watches ---------------------------------------------------------------------


def _watch_description(w: sqlite3.Row) -> str:
    what = f"topic {w['topic']}" if w["topic"] else (f"keywords: {w['keywords']}" if w["keywords"] else "everything")
    where = ", ".join(
        f"{k} {w[k]}" for k in ("level", "state", "domain", "search") if w[k]
    ) or "all crawled sites"
    return f"{what} on {where}" + ("" if w["books_only"] else " (all documents)")


def cmd_watch_add(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    if args.topic:
        make_matcher(args.topic, [], args.config)  # fails early on an unknown topic
    db.add_watch(
        conn,
        {"name": args.name, "topic": args.topic, "keywords": args.keywords, "level": args.level,
         "state": args.state, "domain": _clean_host(args.domain) if args.domain else None, "search": args.search,
         "books_only": int(not args.all_documents)},
    )  # fmt: skip
    say(f"Watching {args.name!r}: {_watch_description(db.list_watches(conn, [args.name])[0])}.")
    say("Run it regularly with: govweb watch run")
    return 0


def cmd_watch_list(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    rows = [
        {"name": w["name"], "what": _watch_description(w), "last_run": w["last_run_at"] or "never"}
        for w in db.list_watches(conn)
    ]
    print_table(rows, [("name", "Name", 20), ("what", "Watching", 70), ("last_run", "Last run", 25)])
    return 0


def cmd_watch_remove(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    ok = db.remove_watch(conn, args.name)
    say("Removed." if ok else f"No watch named {args.name!r}.")
    return 0 if ok else 1


def cmd_watch_run(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    watches = db.list_watches(conn, args.names or None)
    if not watches:
        say("No watches. Add one with: govweb watch add NAME --topic TOPIC (or --keywords ...)")
        return 1
    if not args.no_crawl:
        cutoff = _since(args.recrawl_days) if args.recrawl_days else None
        due: dict[str, sqlite3.Row] = {}
        for w in watches:
            for site in db.select_sites(
                conn, domains=[w["domain"]] if w["domain"] else None, level=w["level"], state=w["state"],
                search=w["search"], crawled_only=True, not_crawled_since=cutoff,
            ):  # fmt: skip
                due.setdefault(site["domain"], site)
        sites = list(due.values())[: args.max_sites]
        if sites:
            args.crawl_keywords = list(dict.fromkeys(
                k for w in watches
                for k in (topic_keywords(args.config, [w["topic"]]) if w["topic"] else keyword_list(w["keywords"]))
            ))  # fmt: skip
            run_crawls(conn, sites, args)
        else:
            say("No sites due for a re-crawl (crawl sites first with govweb crawl, or lower --recrawl-days).")
    stamp = db.now()
    sections = []
    for w in watches:
        docs = db.select_documents(
            conn, domain=w["domain"], level=w["level"], state=w["state"], search=w["search"],
            books_only=bool(w["books_only"]), new_since=w["last_run_at"] or w["created_at"],
        )  # fmt: skip
        found = rank(docs, make_matcher(w["topic"], keyword_list(w["keywords"]), args.config), args.reusable)
        sections.append((w["name"], _watch_description(w), found))
        say(f"\n== {w['name']}: {len(found)} new ==")
        for item in found[:20]:
            say(f"  [{item.rights.status}] {item.doc['title'][:70]}  {item.doc['url']}")
        if len(found) > 20:
            say(f"  ... {len(found) - 20} more (use --out for the full list)")
        db.mark_watch_run(conn, w["name"], stamp)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        if args.out.suffix == ".csv":
            with args.out.open("w", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=["watch", *DOC_COLUMNS], extrasaction="ignore")
                writer.writeheader()
                for name, _, found in sections:
                    for f in found:
                        writer.writerow({**dict(f.doc), "watch": name, "score": f.score, "rights": f.rights.status,
                                         "rights_note": f.rights.note})  # fmt: skip
        else:
            args.out.write_text(markdown_report(sections, stamp))
        say(f"\nWrote the report to {args.out}")
    return 0


# --- Amazon, results export, Google Sheets ---------------------------------------------


def cmd_amazon(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    from govbooks.config import load_config
    from govbooks.market import build_provider

    config = load_config(args.config)
    provider = build_provider(args.provider or config.amazon_provider, make_json_http(), config)
    docs = _narrow(db.documents_to_check(conn, older_than=_since(args.recheck_days)), args)
    topics = topic_matchers(args.config)
    if topics:  # books on the searched topics first, then the rest, newest first
        on_topic = {d["url"] for d in docs if matching_topics(d, topics)}
        if args.topics_only:
            docs = [d for d in docs if d["url"] in on_topic]
        docs.sort(key=lambda d: d["url"] not in on_topic)
    docs = docs[: args.limit]
    if not docs:
        say("Nothing to check: every book-like document was checked recently.")
        return 0
    say(f"Checking {len(docs)} book-like document(s) on Amazon with {provider.name}...")
    checked, found = check_documents(conn, provider, docs, progress=say if args.verbose else None)
    say(f"Checked {checked}: {found} already on Amazon, {checked - found} not found.")
    return 0


def _narrow(rows: list[sqlite3.Row], args: argparse.Namespace) -> list[sqlite3.Row]:
    """Only documents on --sites (or their sub-sites), or on --state's own sites, whose title or
    file name has one of --keywords; never documents on the excluded sites and paths, or on the
    sites of the states whose publications can't be reused."""
    rows = [r for r in rows if not is_excluded(r["url"]) and not is_excluded_state_site(r["level"], r["state"])]
    if getattr(args, "state", None):
        rows = [r for r in rows if r["level"] == "state" and (r["state"] or "") == args.state.upper()]
    sites = [_clean_host(s) for s in args.sites or []]
    if sites:
        rows = [r for r in rows if any(r["domain"] == s or r["domain"].endswith("." + s) for s in sites)]
    keywords = keyword_list(args.keywords)
    if keywords:
        matcher = make_matcher(None, keywords)
        rows = [r for r in rows if matcher(r)]
    return rows


def cmd_count_pages(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    """Download PDFs whose pages haven't been counted (books on the topics first), count them, and
    tell whether each opens with a cover (when pypdfium2 is installed)."""
    from govweb import covers
    from govweb.pdfpages import count_pages

    looks = covers.available()
    docs = _narrow(db.documents_to_count(conn, books_only=not args.all_documents, covers=looks), args)
    # One version of each book: not the same guide in twenty languages.
    docs = [pick_version(g, itemgetter("title"), itemgetter("url"))
            for g in variant_groups(docs, itemgetter("title"), itemgetter("url"), itemgetter("domain"))]  # fmt: skip
    topics = topic_matchers(args.config)
    on_topic = {d["url"] for d in docs if matching_topics(d, topics)} if topics else set()
    if args.topics_only and topics:
        docs = [d for d in docs if d["url"] in on_topic]
    docs.sort(key=lambda d: (d["url"] not in on_topic, -d["book_score"]))  # stable: newest first in each group
    docs = docs[: args.limit]
    if not docs:
        say("No PDFs to count.")
        return 0
    say(f"Counting the pages of {len(docs)} PDF(s)...")
    fetcher = make_fetcher(args)
    counted = 0
    for doc in docs:
        pages, cover, year = 0, None, None
        if fetcher.allowed(doc["url"]):
            fetched = fetcher.get(doc["url"], max_bytes=int(args.max_mb * 1_000_000))
            if fetched.ok:
                pages = count_pages(fetched.body) or 0
                cover, year = covers.look_at(fetched.body) if looks else (None, None)
        db.save_pages(conn, doc["url"], pages, (cover or "") if looks else None, (year or 0) if looks else None)
        counted += bool(pages)
        if args.verbose:
            say(f"  {pages or '?':>5}  {year or '':>4}  {covers.LABELS.get(cover or '', '?'):<15}  {doc['title'][:65]}")
    say(f"Counted {counted} of {len(docs)}.")
    return 0


def cmd_export(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    rows = db.export_rows(
        conn, books_only=not args.all_documents, since=_since(args.days) if args.days else None, new_only=args.new_only
    )
    results = dedupe(result_rows(_narrow(rows, args), topics=topic_matchers(args.config)))
    if args.reusable:
        results = [r for r in results if r["rights"] in copyright_policy.REUSABLE]
    if args.topics_only:
        results = [r for r in results if r["topics"]]
    if args.min_pages:  # documents whose pages couldn't be counted stay
        results = [r for r in results if not r["pages"] or int(r["pages"]) >= args.min_pages]
    if args.sort == "pages":
        results.sort(key=book_order)
    elif args.sort == "year":
        results.sort(key=year_order)
    if args.top:
        results = results[: args.top]
    if args.out.suffix == ".md":
        results.sort(key=lambda r: not r["topics"])  # stable: topic books first, each group newest first
        write_markdown(results, args.out, args.heading or "Books found", limit=args.rows)
    else:
        write_csv(results, args.out, sheet_search=args.sheet)
    say(f"Wrote {len(results)} row(s) to {args.out}")
    return 0


def cmd_merge(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    """Join results tables written by export (one per state, say) into one CSV, or a .md table."""
    from govweb.export import merge_csv

    try:
        header, rows = merge_csv(sorted(args.files))
    except ValueError as exc:
        say(f"Error: {exc}")
        return 1
    if args.sort_by:
        if args.sort_by not in header:
            say(f"Error: no column {args.sort_by!r}")
            return 1
        column = header.index(args.sort_by)
        rows.sort(key=lambda r: -int(r[column]) if r[column].isdigit() else 1)  # biggest first, blanks last
    if args.out.suffix == ".md":
        write_markdown([dict(zip(header, r)) for r in rows], args.out, args.heading or "Books found", limit=args.rows)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows([header, *rows])
    say(f"Wrote {len(rows)} row(s) from {len(args.files)} file(s) to {args.out}")
    return 0


def cmd_track_books(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    """Watch the sheet's books for new versions (govweb.versions): its "Tracked books" tab."""
    import os

    from govweb import versions
    from govweb.sheets import SheetsError, authorized_session, read_tab, tab_titles, upload

    credentials = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    sheet_id = args.sheet_id or os.environ.get("GOOGLE_SHEET_ID", "").strip()
    if not credentials or not sheet_id:
        say("Needs GOOGLE_SERVICE_ACCOUNT_JSON in the environment and --sheet-id (or GOOGLE_SHEET_ID).")
        return 1
    today = versions.today_utc()
    try:
        session = authorized_session(credentials)
        titles = tab_titles(session, sheet_id)
        tracked = versions.from_tracked_tab(read_tab(session, sheet_id, versions.TAB)) if versions.TAB in titles else []
        found = [book for title in titles if title.startswith(versions.SOURCE_TABS)
                 for book in versions.from_search_tab(read_tab(session, sheet_id, title), args.min_pages)]  # fmt: skip
        if args.watchlist and args.watchlist.exists():
            with args.watchlist.open(newline="", encoding="utf-8") as fh:
                found += versions.from_watchlist(list(csv.reader(fh)))
    except SheetsError as exc:
        say(f"Error: {exc}")
        return 2
    books = versions.merge(tracked, found, today)
    due = sorted((b for b in books if b.active), key=lambda b: b.last_checked)[: args.limit]
    say(f"Tracking {len(books)} book(s) ({len(books) - len(tracked)} new); checking {len(due)}...")
    fetcher = make_fetcher(args)
    events = []
    for book in due:
        versions.check(book, fetcher, today)
        events += book.events
        if args.verbose:
            say(f"  {book.status[:40]:<40}  {book.title[:70]}")
    try:
        upload(session, sheet_id, {versions.TAB: [versions.COLUMNS] + [b.row() for b in books]}, formulas=True)
    except SheetsError as exc:
        say(f"Error: {exc}")
        return 2
    lines = [f"## New versions of tracked books ({today})", "",
             f"{len(due)} of {len(books)} tracked book(s) checked; {len(events)} change(s).", ""]  # fmt: skip
    lines += [f"- {event}" for event in events]
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    say("\n".join(lines))
    return 0


def cmd_sheet(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    import os

    from govweb.sheets import SheetsError, authorized_session, read_csv, upload

    credentials = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    sheet_id = args.sheet_id or os.environ.get("GOOGLE_SHEET_ID", "").strip()
    if not credentials or not sheet_id:
        say("Needs GOOGLE_SERVICE_ACCOUNT_JSON in the environment and --sheet-id (or GOOGLE_SHEET_ID).")
        return 1
    tabs = {}
    for spec in args.tab:
        title, _, path = spec.partition("=")
        if not path or not Path(path).exists():
            say(f"Skipping tab {title!r}: no file {path!r}")
            continue
        tabs[title] = read_csv(Path(path))
    try:
        upload(authorized_session(credentials), sheet_id, tabs, formulas=args.formulas)
    except SheetsError as exc:
        say(f"Error: {exc}")
        return 2
    say(f"Updated {len(tabs)} tab(s): {', '.join(tabs)}")
    return 0


# --- copyright --------------------------------------------------------------------


def cmd_copyright(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    if args.state:
        policy = copyright_policy.state_policy(args.state)
        if policy is None:
            say(f"No policy on file for {args.state}.")
            return 1
        say(f"{policy.state} ({policy.abbr}): {policy.status.replace('_', ' ')}, {policy.confidence} confidence")
        say(f"  {policy.summary}")
        if policy.key_law:
            say(f"  Law: {policy.key_law}")
        for source in policy.sources:
            say(f"  Source: {source}")
        return 0
    say("Federal: " + copyright_policy.FEDERAL_RULE)
    say("Federal exceptions:")
    for exc in copyright_policy.FEDERAL_EXCEPTIONS:
        say(f"  - {exc.name}: {exc.summary}")
    say("")
    rows = [
        {"abbr": p.abbr, "state": p.state, "status": p.status.replace("_", " "), "confidence": p.confidence,
         "summary": p.summary}
        for p in sorted(copyright_policy.state_policies().values(), key=lambda p: p.state)
    ]  # fmt: skip
    print_table(rows, [("abbr", "St", 2), ("state", "State", 14), ("status", "Policy", 16), ("confidence", "Conf.", 6),
                       ("summary", "Summary", 90)])  # fmt: skip
    say("\nA first screening, not legal advice. Details: govweb copyright --state XX")
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


def _crawl_options(p: argparse.ArgumentParser, max_pages: int = 100) -> None:
    p.add_argument("--max-pages", type=int, default=max_pages, help=f"pages fetched per site (default {max_pages})")
    p.add_argument("--max-depth", type=int, default=3, help="link depth from the home page (default 3)")
    p.add_argument("--no-sitemaps", action="store_true", help="don't read sitemaps")
    p.add_argument("--workers", type=int, default=4, help="sites crawled in parallel (each site one at a time)")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests to one host (default 1)")
    p.add_argument("--max-minutes", type=float, help="stop each site after this many minutes")


def _narrow_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--sites", nargs="+", action="extend", help="only documents on these sites and their sub-sites")
    p.add_argument("--state", help="only documents on a state's own sites (two-letter code, e.g. TX)")
    p.add_argument("--keywords", help="only documents whose title has one of these comma-separated words or phrases")


def _document_filters(p: argparse.ArgumentParser) -> None:
    p.add_argument("--domain")
    p.add_argument("--level", choices=LEVEL_NAMES)
    p.add_argument("--state")
    p.add_argument("--type", help="pdf, epub, mobi, docx, ...")
    p.add_argument("--search", help="title or URL contains this text")
    p.add_argument("--topic", help="a topic from govbooks.toml (keeps and ranks matching documents)")
    p.add_argument("--keywords", help="comma-separated words or phrases (instead of a topic)")
    p.add_argument("--config", type=Path, help="govbooks config for --topic (default: ./govbooks.toml)")
    p.add_argument("--books-only", action="store_true", help="skip forms, agendas, minutes and other paperwork")
    p.add_argument("--reusable", action="store_true", help="only works that screen as free to reuse")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--out", type=Path, help="write all matches to a CSV file")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="govweb",
        description="Map US government websites, find the books they host, and watch for new ones.",
    )
    parser.add_argument("--db", type=Path, default=Path("govweb.db"), help="database file (default: govweb.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="download the official .gov domain registry (CISA)")
    p.add_argument("--url", default=REGISTRY_URL)
    p.add_argument("--file", type=Path, help="read a local copy of the registry CSV instead")
    p.set_defaults(func=cmd_sync)

    agencies = sub.add_parser("agencies", help="federal and state agency websites").add_subparsers(
        dest="agencies_command", required=True
    )
    p = agencies.add_parser("sync", help="add agency websites (Federal Register + Wikidata) to the map")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--federal-only", action="store_true")
    group.add_argument("--states-only", action="store_true")
    p.add_argument("--state", action="append", help="only these states (repeatable, e.g. --state CA)")
    p.set_defaults(func=cmd_agencies_sync)
    p = agencies.add_parser("list", help="agencies with their websites and crawl results")
    p.add_argument("--level", choices=["federal", "state"])
    p.add_argument("--state")
    p.add_argument("--search")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_agencies_list)

    p = sub.add_parser("add", help="add any website to the map (it can then be crawled and watched)")
    p.add_argument("url", help="e.g. https://www.army.mil or dot.state.tx.us")
    p.add_argument("--name", help="who runs it")
    p.add_argument("--level", choices=LEVEL_NAMES)
    p.add_argument("--state", help="state it belongs to")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("stats", help="domains, crawled sites and documents per level")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("sites", help="list sites")
    _site_filters(p)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_sites)

    p = sub.add_parser("map", help="the sites as a tree: level > state or organization > site")
    _site_filters(p)
    p.add_argument("--depth", type=int, help="stop the tree at this depth (0 = levels only)")
    p.add_argument("--out", type=Path, help="write the tree as JSON instead of printing it")
    p.set_defaults(func=cmd_map)

    p = sub.add_parser("crawl", help="crawl sites for publication pages and documents")
    p.add_argument("domains", nargs="*", help="specific sites, e.g. usda.gov or https://www.army.mil")
    _site_filters(p)
    p.add_argument("--all", action="store_true", help="every site matching the filters (or everything)")
    p.add_argument("--limit", type=int, help="at most this many sites")
    _crawl_options(p)
    p.add_argument("--skip-recent-days", type=int, default=30, help="skip sites crawled in the last N days")
    p.add_argument("--topic", action="append", help="follow links about this govbooks.toml topic first (repeatable)")
    p.add_argument("--all-topics", action="store_true", help="follow links about any topic in govbooks.toml first")
    p.add_argument("--keywords", help="comma-separated words or phrases whose links are followed first")
    p.add_argument("--config", type=Path, help="govbooks config for topics (default: ./govbooks.toml)")
    p.add_argument("--with-subsites", action="store_true",
                   help="then crawl the sub-sites found under them (army.mil -> history.army.mil, ...)")
    p.add_argument("--max-subsites", type=int, default=30, help="sub-sites crawled per run (default 30)")
    p.add_argument("--subsite-pages", type=int, help="pages fetched per sub-site (default: --max-pages)")
    p.add_argument("--subsite-minutes", type=float, help="minutes per sub-site at most (default: --max-minutes)")
    p.add_argument("--subsites-only", action="store_true", help="crawl only the sub-sites of the chosen sites")
    p.add_argument("--prefer-names", metavar="WORDS",
                   help='comma-separated parts of site names crawled first, e.g. "librar,archiv,refugee" '
                        '(the state\'s portal still leads); "-court" puts names with "court" last')
    p.set_defaults(func=cmd_crawl)

    p = sub.add_parser("portals", help="each state's official website, where its search starts")
    p.add_argument("states", nargs="*", help="put these states' portals on the map and print them, e.g. TX")
    p.set_defaults(func=cmd_portals)

    p = sub.add_parser("ntrs", help="search NASA's Technical Reports Server (reports with their PDFs)")
    p.add_argument("query", help='words to search for, e.g. "Apollo 13"')
    p.add_argument("--limit", type=int, default=300, help="at most this many reports (default 300)")
    p.set_defaults(func=cmd_ntrs)

    p = sub.add_parser("docs", help="documents found by crawling")
    _document_filters(p)
    p.set_defaults(func=cmd_docs)

    p = sub.add_parser("new", help="documents that appeared on re-crawls recently")
    _document_filters(p)
    p.add_argument("--days", type=int, default=7, help="look back this many days (default 7)")
    p.add_argument("--since", help="or since this ISO date/time, e.g. 2026-09-01")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("pages", help="publication listing pages found by crawling")
    p.add_argument("--domain")
    p.add_argument("--min-hint", type=int, default=1)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_pages)

    watch = sub.add_parser("watch", help="saved searches that report new books").add_subparsers(
        dest="watch_command", required=True
    )
    p = watch.add_parser("add", help="save a search (same name replaces it)")
    p.add_argument("name")
    what = p.add_mutually_exclusive_group()
    what.add_argument("--topic", help="a topic from govbooks.toml")
    what.add_argument("--keywords", help="comma-separated words or phrases")
    p.add_argument("--level", choices=LEVEL_NAMES)
    p.add_argument("--state")
    p.add_argument("--domain", help="one site")
    p.add_argument("--search", help="organization or domain contains this text")
    p.add_argument("--all-documents", action="store_true", help="report paperwork too, not only book-like files")
    p.add_argument("--config", type=Path, help="govbooks config for --topic")
    p.set_defaults(func=cmd_watch_add)
    p = watch.add_parser("list", help="saved searches")
    p.set_defaults(func=cmd_watch_list)
    p = watch.add_parser("remove", help="delete a saved search")
    p.add_argument("name")
    p.set_defaults(func=cmd_watch_remove)
    p = watch.add_parser("run", help="re-crawl the watched sites and report new matching books")
    p.add_argument("names", nargs="*", help="only these watches (default: all)")
    p.add_argument("--no-crawl", action="store_true", help="only report what earlier crawls found")
    p.add_argument("--recrawl-days", type=int, default=7, help="re-crawl sites last crawled over N days ago")
    p.add_argument("--max-sites", type=int, default=200, help="re-crawl at most this many sites")
    _crawl_options(p, max_pages=60)
    p.add_argument("--reusable", action="store_true", help="only works that screen as free to reuse")
    p.add_argument("--config", type=Path, help="govbooks config for topics")
    p.add_argument("--out", type=Path, help="write a report (.md or .csv)")
    p.set_defaults(func=cmd_watch_run)

    p = sub.add_parser("amazon", help="check whether book-like documents are already sold on Amazon")
    p.add_argument("--provider", choices=["catalog", "keepa", "creators"], help="default: from govbooks.toml")
    p.add_argument("--limit", type=int, default=200, help="at most this many documents per run")
    p.add_argument("--recheck-days", type=int, default=30, help="re-check documents checked over N days ago")
    p.add_argument("--config", type=Path, help="govbooks config (Amazon provider, marketplace, topics)")
    p.add_argument("--topics-only", action="store_true", help="only documents matching a topic (they go first anyway)")
    _narrow_options(p)
    p.add_argument("--verbose", action="store_true", help="print each search")
    p.set_defaults(func=cmd_amazon)

    p = sub.add_parser("count-pages", help="count the pages of PDFs (downloads them; books on the topics first)")
    p.add_argument("--limit", type=int, default=100, help="at most this many PDFs (default 100)")
    p.add_argument("--max-mb", type=float, default=80, help="skip the rest of a PDF over this size (default 80 MB)")
    p.add_argument("--config", type=Path, help="govbooks config whose topics go first")
    p.add_argument("--topics-only", action="store_true", help="only PDFs matching a topic")
    p.add_argument("--all-documents", action="store_true", help="include paperwork, not only book-like files")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests to one host (default 1)")
    p.add_argument("--verbose", action="store_true", help="print each count")
    _narrow_options(p)
    p.set_defaults(func=cmd_count_pages)

    p = sub.add_parser("export", help="write the results table (books, topics, publisher, rights, Amazon status)")
    p.add_argument("--out", type=Path, required=True, help="a .csv file, or .md for a short readable table")
    p.add_argument("--config", type=Path, help="govbooks config whose topics fill the topics column")
    p.add_argument("--topics-only", action="store_true", help="only documents matching a topic")
    p.add_argument("--heading", help="title of the .md table")
    p.add_argument("--rows", type=int, default=50, help="rows in the .md table (default 50)")
    p.add_argument("--sheet", metavar="SEARCH", help="write the Google Sheet layout, with SEARCH in its first column")
    p.add_argument("--sort", choices=["newest", "pages", "year"], default="newest",
                   help="newest found first (default); pages: the longest first; year: the latest published "
                        "first (both with paperwork last)")
    p.add_argument("--top", type=int, help="only the first N rows (after sorting)")
    p.add_argument("--min-pages", type=int, help="leave out documents shorter than this (uncounted ones stay)")
    _narrow_options(p)
    p.add_argument("--days", type=int, help="only documents first found in the last N days")
    p.add_argument("--new-only", action="store_true", help="only documents that appeared after a site's first crawl")
    p.add_argument("--all-documents", action="store_true", help="include paperwork, not only book-like files")
    p.add_argument("--reusable", action="store_true", help="only works that screen as free to reuse")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("merge", help="join results tables written by export into one CSV or .md table")
    p.add_argument("files", nargs="+", type=Path, help="CSV files with the same columns")
    p.add_argument("--out", type=Path, required=True, help="a .csv file, or .md for a readable table (needs the "
                   "export columns, not the sheet layout)")
    p.add_argument("--heading", help="title of the .md table")
    p.add_argument("--rows", type=int, default=300, help="rows in the .md table (default 300)")
    p.add_argument("--sort-by", metavar="COLUMN", help='biggest number first in this column, e.g. "year" or "Year"')
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("track-books", help='watch the Google Sheet\'s books for new versions ("Tracked books" tab)')
    p.add_argument("--sheet-id", help="the id in the sheet's URL (default: GOOGLE_SHEET_ID)")
    p.add_argument("--min-pages", type=int, default=10, help="track the search tabs' books of this many pages or more")
    p.add_argument("--limit", type=int, default=500, help="books checked per run, the least recently checked first")
    p.add_argument("--watchlist", type=Path, help="a .csv of books to track too (Title, Link, Found on (page), "
                   "Authority, Source website, Pages, Notes)")
    p.add_argument("--summary", type=Path, help="write what changed to this .md file")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests to one host (default 1)")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_track_books)

    p = sub.add_parser("sheet", help="upload CSV results into Google Sheet tabs (service account)")
    p.add_argument("--sheet-id", help="the id in the sheet's URL (default: GOOGLE_SHEET_ID)")
    p.add_argument("--tab", action="append", required=True, help='"Tab name=path.csv" (repeatable)')
    p.add_argument("--formulas", action="store_true",
                   help="enter formulas (for CSVs written by export --sheet, whose titles are links)")
    p.set_defaults(func=cmd_sheet)

    p = sub.add_parser("copyright", help="copyright policy: federal rules and exceptions, and each state")
    p.add_argument("--state", help="details for one state, e.g. CA")
    p.set_defaults(func=cmd_copyright)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if isinstance(getattr(args, "state", None), str):
        args.state = STATE_ABBR[resolve_state(args.state)]
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
