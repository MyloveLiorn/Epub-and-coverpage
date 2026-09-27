"""govweb: agency directory import, any-site crawling, new-book tracking, watches and copyright."""

import sqlite3
from pathlib import Path

import pytest

from govbooks import copyright_policy
from govbooks.agencies.federal_register import parse_agencies
from govbooks.agencies.wikidata import parse_state_agencies
from govbooks.copyright_policy import StatePolicy, assess
from govweb import cli, db
from govweb.agencies import import_agencies, site_host
from govweb.classify import book_score
from govweb.crawl import CrawlLimits, crawl_site
from govweb.registry import parse_registry
from tests.conftest import FEDERAL_REGISTER, WIKIDATA_CALIFORNIA, FakeHttp
from tests.test_govweb import REGISTRY_CSV, ROBOTS, SITE, FakeFetcher, html

POLICIES = {
    "CA": StatePolicy("CA", "California", "public_domain", "Most California records are free to reuse."),
    "TX": StatePolicy("TX", "Texas", "claims_copyright", "Texas agencies may hold copyright."),
    "OH": StatePolicy("OH", "Ohio", "unclear", "No clear rule."),
}


@pytest.fixture(autouse=True)
def policies(monkeypatch):
    monkeypatch.setattr(copyright_policy, "state_policies", lambda: POLICIES)


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.sync_sites(connection, parse_registry(REGISTRY_CSV))
    yield connection
    connection.close()


# --- copyright screening ---------------------------------------------------------------


def test_copyright_federal_rule_and_exceptions():
    assert assess("federal", names=["Department of Agriculture"]).status == "likely_public_domain"
    usps = assess("federal", names=["United States Postal Service"])
    assert usps.status == "check" and "stamp" in usps.note
    assert assess("federal", domain="www.stlouisfed.org").status == "check"
    assert assess("federal", names=["Smithsonian Institution"]).status == "check"
    assert assess("federal", year=1920).status == "public_domain"


def test_copyright_state_policies():
    assert assess("state", state="CA").status == "likely_public_domain"
    assert assess("state", state="Texas").status == "likely_copyrighted"
    assert assess("state", state="OH").status == "check"
    assert assess("state", state="WY").status == "check"  # no policy on file
    assert assess("state", state="CA", names=["UC Cooperative Extension"]).status == "check"
    local = assess("county", state="CA")
    assert local.status == "check" and "public domain" in local.note
    assert assess("tribal").status == "check"
    assert assess(None).status == "unknown"


def test_govbooks_uses_state_policy():
    from govbooks.discover import assess_rights

    assert assess_rights("state", 2001, None, jurisdiction="California")[0] == "likely_public_domain"
    assert assess_rights("state", 2001, None, jurisdiction="Texas")[0] == "likely_copyrighted"


# --- classification and storage ----------------------------------------------------


def test_book_score():
    assert book_score("Complete Guide to Home Canning", "https://x.gov/a.pdf") > 0
    assert book_score("", "https://x.gov/files/Beekeeping_Handbook.pdf") > 0
    assert book_score("Board meeting agenda and minutes", "https://x.gov/a.pdf") < 0
    assert book_score("Building permit application form", "https://x.gov/a.pdf") < 0
    assert book_score("Untitled", "https://x.gov/doc1.pdf") == 0


def test_old_databases_are_upgraded(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE sites (domain TEXT PRIMARY KEY, domain_type TEXT NOT NULL, level TEXT NOT NULL,
            election INTEGER NOT NULL DEFAULT 0, organization TEXT NOT NULL, suborganization TEXT, city TEXT,
            state TEXT, parent_domain TEXT, in_registry INTEGER NOT NULL DEFAULT 1, synced_at TEXT NOT NULL,
            crawled_at TEXT, crawl_status TEXT, home_url TEXT, pages_crawled INTEGER, documents_found INTEGER,
            sitemap_urls INTEGER, error TEXT);
        CREATE TABLE documents (url TEXT PRIMARY KEY, domain TEXT NOT NULL, title TEXT NOT NULL,
            file_type TEXT NOT NULL, found_on TEXT, found_via TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
        INSERT INTO sites (domain, domain_type, level, organization, parent_domain, synced_at, crawled_at)
            VALUES ('ars.usda.gov', 'x', 'federal', 'USDA', 'usda.gov', 't', '2026-01-01');
        INSERT INTO documents VALUES ('https://x.gov/Field_Guide.pdf', 'ars.usda.gov', 'Field Guide', 'pdf',
            NULL, NULL, 't', 't');
        """
    )
    old.commit()
    old.close()
    conn = db.connect(path)
    site = db.get_site(conn, "ars.usda.gov")
    assert (site["source"], site["first_crawled_at"]) == ("subdomain", "2026-01-01")
    assert conn.execute("SELECT book_score FROM documents").fetchone()[0] > 0


# --- agency directory -----------------------------------------------------------------


def test_site_host():
    assert site_host("https://www.fs.usda.gov/about") == "fs.usda.gov"
    assert site_host("water.ca.gov") == "water.ca.gov"
    assert site_host("http://www.dot.state.tx.us:8080/") == "dot.state.tx.us"
    assert site_host("") is None and site_host("localhost") is None


def test_import_agencies_fills_the_map(conn):
    federal = parse_agencies(
        [*FEDERAL_REGISTER, {"id": 30, "name": "Army Department", "short_name": "Army", "parent_id": None,
                             "agency_url": "https://www.army.mil/"}]
    )  # fmt: skip
    state = parse_state_agencies("California", WIKIDATA_CALIFORNIA)
    stats = import_agencies(conn, federal + state)

    assert stats.new_sites == 2  # army.mil and epa.gov are outside this small registry
    assert stats.new_subsites == 2  # fs.usda.gov under usda.gov, water.ca.gov under ca.gov
    army = db.get_site(conn, "army.mil")
    assert (army["source"], army["level"], army["organization"]) == ("agency", "federal", "Army Department")
    fs = db.get_site(conn, "fs.usda.gov")
    assert (fs["parent_domain"], fs["organization"], fs["suborganization"]) == (
        "usda.gov", "Department of Agriculture", "Forest Service",
    )  # fmt: skip
    water = db.get_site(conn, "water.ca.gov")
    assert (water["state"], water["suborganization"]) == ("CA", "California Department of Water Resources")
    listed = {r["id"]: r for r in db.select_agencies(conn, state="CA")}
    assert listed["wd:Q5020016"]["host"] == "water.ca.gov"


def test_cli_agencies_sync(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "registry.csv").write_text(REGISTRY_CSV)
    http = FakeHttp()
    http.add("GET", "federalregister.gov", FEDERAL_REGISTER)
    monkeypatch.setattr(cli, "make_json_http", lambda: http)
    cli.main(["--db", "w.db", "sync", "--file", "registry.csv"])
    assert cli.main(["--db", "w.db", "agencies", "sync", "--federal-only"]) == 0
    out = capsys.readouterr().out
    assert "3 agencies, 3 with a website" in out
    cli.main(["--db", "w.db", "agencies", "list", "--search", "forest"])
    assert "fs.usda.gov" in capsys.readouterr().out


# --- crawling anything, re-crawls and new books ----------------------------------------------


def test_recrawl_starts_from_known_listing_pages():
    pages = {
        "https://x.gov/": (200, "text/html", html("Home", ("/a", "A"), ("/b", "B"))),
        "https://x.gov/a": (200, "text/html", html("A")),
        "https://x.gov/b": (200, "text/html", html("B")),
        "https://x.gov/deep/pubs": (200, "text/html", html("Pubs", ("/new-guide.pdf", "New Field Guide"))),
    }
    result = crawl_site("x.gov", FakeFetcher(pages), CrawlLimits(max_pages=2, use_sitemaps=False),
                        seeds=["https://x.gov/deep/pubs"])  # fmt: skip
    assert [p.url for p in result.pages] == ["https://x.gov/", "https://x.gov/deep/pubs"]
    assert "https://x.gov/new-guide.pdf" in result.documents


def test_new_documents_exclude_the_first_crawl(conn):
    fetcher = FakeFetcher(dict(SITE), ROBOTS)
    db.store_result(conn, crawl_site("usda.gov", fetcher, CrawlLimits(max_pages=10)))
    assert db.select_documents(conn, new_since="2000-01-01") == []  # the first crawl is the baseline

    conn.execute("UPDATE sites SET first_crawled_at = '2026-01-01T00:00:00+00:00'")
    conn.execute("UPDATE documents SET first_seen = '2026-01-01T00:00:00+00:00'")
    SITE_V2 = {**SITE, "https://www.usda.gov/media/publications": (200, "text/html", html(
        "Publications", ("/files/Beekeeping_Handbook_2026.pdf", "Beekeeping Handbook"),
        ("/files/meeting-agenda.pdf", "Meeting agenda"),
    ))}  # fmt: skip
    db.store_result(conn, crawl_site("usda.gov", FakeFetcher(SITE_V2, ROBOTS), CrawlLimits(max_pages=10)))
    new = db.select_documents(conn, new_since="2026-06-01", books_only=True)
    assert [d["title"] for d in new] == ["Beekeeping Handbook"]


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "registry.csv").write_text(REGISTRY_CSV)
    site = dict(SITE)
    monkeypatch.setattr(cli, "make_fetcher", lambda args: FakeFetcher(site, ROBOTS))
    cli.main(["--db", "w.db", "sync", "--file", "registry.csv"])
    return site


def run(capsys, *argv):
    code = cli.main(["--db", "w.db", *argv])
    return code, capsys.readouterr().out


def test_cli_crawls_any_site(cli_env, capsys):
    cli_env["https://example.org/"] = (200, "text/html", html("Club", ("/bees-handbook.pdf", "Bees handbook")))
    code, out = run(capsys, "crawl", "https://www.example.org/")
    assert "example.org is not a known government site; added it" in out
    assert "example.org: 1 pages (0 publication pages), 1 documents" in out
    run(capsys, "add", "example.org", "--name", "Beekeepers Club", "--state", "CA")
    code, out = run(capsys, "sites", "--search", "Beekeepers")
    assert "example.org" in out and "other" in out


def test_cli_watch_reports_only_new_books(cli_env, capsys):
    run(capsys, "crawl", "usda.gov")
    run(capsys, "watch", "add", "bees", "--keywords", "beekeeping, honey bee", "--level", "federal")
    code, out = run(capsys, "watch", "list")
    assert "keywords: beekeeping, honey bee on level federal" in out

    code, out = run(capsys, "watch", "run", "--recrawl-days", "0", "--out", "report.md")
    assert "== bees: 0 new ==" in out  # books from the first crawl are the baseline

    cli_env["https://www.usda.gov/media/publications"] = (200, "text/html", html(
        "Publications", ("/files/Beekeeping_Handbook_2026.pdf", "Beekeeping Handbook, 2026 edition"),
        ("/files/bee-meeting-agenda.pdf", "Beekeeping committee meeting agenda"),
    ))  # fmt: skip
    code, out = run(capsys, "watch", "run", "--recrawl-days", "0", "--out", "report.md")
    assert "== bees: 1 new ==" in out
    assert "[likely_public_domain] Beekeeping Handbook, 2026 edition" in out
    report = Path("report.md").read_text()
    assert "Beekeeping Handbook, 2026 edition" in report and "agenda" not in report

    code, out = run(capsys, "new", "--days", "1", "--keywords", "beekeeping")
    assert "2 new document(s)" in out  # without --books-only the agenda counts too
    code, out = run(capsys, "new", "--days", "1", "--keywords", "beekeeping", "--books-only")
    assert "1 new document(s)" in out

    code, out = run(capsys, "watch", "run", "--no-crawl")
    assert "== bees: 0 new ==" in out  # already reported
    assert run(capsys, "watch", "remove", "bees")[0] == 0


def test_cli_copyright(cli_env, capsys):
    code, out = run(capsys, "copyright")
    assert "Federal exceptions:" in out and "US Postal Service" in out and "Texas" in out
    code, out = run(capsys, "copyright", "--state", "tx")
    assert "Texas (TX): claims copyright" in out
