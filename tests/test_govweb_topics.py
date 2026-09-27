"""govweb: searching websites for topics, crawling sub-sites, and topics in the results."""

import csv

import pytest

from govbooks import copyright_policy
from govbooks.agencies.federal_register import parse_agencies
from govweb import cli, db
from govweb.agencies import guess_owner, import_agencies
from govweb.classify import host_hint_score, topic_phrases, topic_score
from govweb.crawl import CrawlLimits, crawl_site
from govweb.registry import parse_registry
from tests.test_govweb import REGISTRY_CSV, FakeFetcher, html
from tests.test_govweb_results import FakeProvider

CONFIG = """
[topics.survival]
keywords = ["survival", "first aid"]
min_score = 3

[topics.beekeeping]
keywords = ["beekeeping", "honey bee"]
min_score = 3
"""

ARMY = {
    "https://army.mil/": (200, "text/html", html(
        "Army",
        ("/news", "News"),
        ("/about", "About"),
        ("/training/field-survival", "Staying alive in the field"),
        ("https://history.army.mil/", "Center of Military History"),
        ("https://home.army.mil/", "Installations"),
    )),  # fmt: skip
    "https://army.mil/news": (200, "text/html", html("News", ("/files/press-release.pdf", "Press release"))),
    "https://army.mil/about": (200, "text/html", html("About")),
    "https://army.mil/training/field-survival": (200, "text/html", html(
        "Survival", ("/files/FM-21-76.pdf", "Survival Field Manual"),
    )),  # fmt: skip
    "https://history.army.mil/": (200, "text/html", html(
        "CMH", ("/books/beekeeping-at-fort-bragg.pdf", "Beekeeping at Fort Bragg: A History"),
    )),  # fmt: skip
    "https://home.army.mil/": (200, "text/html", html("Installations", ("/maps/guide.pdf", "Visitor Guide"))),
}


@pytest.fixture(autouse=True)
def no_state_policies(monkeypatch):
    monkeypatch.setattr(copyright_policy, "state_policies", lambda: {})


def test_topic_and_sub_site_scores():
    phrases = topic_phrases(["beekeeping", "honey bee", "first aid"])
    assert topic_score("https://x.gov/topics/honey-bees/", "", phrases) == 1  # plurals and dashes
    assert topic_score("https://x.gov/a", "First Aid and Beekeeping", phrases) == 2
    assert topic_score("https://x.gov/beekeeping", "", []) == 0
    assert host_hint_score("armypubs.army.mil") > 0
    assert host_hint_score("history.army.mil") > 0
    assert host_hint_score("nal.usda.gov") > 0
    assert host_hint_score("home.army.mil") == host_hint_score("www.army.mil") == 0
    assert host_hint_score("beekeeping.example.gov", phrases) == 1


def test_crawl_follows_topic_links_first():
    # One page besides the home page: without topics it goes to the first link; with topics,
    # to the link that mentions one, although nothing about it looks like a publication page.
    plain = crawl_site("army.mil", FakeFetcher(ARMY), CrawlLimits(max_pages=2, use_sitemaps=False))
    assert [p.url for p in plain.pages][1] == "https://army.mil/news"
    guided = crawl_site(
        "army.mil", FakeFetcher(ARMY), CrawlLimits(max_pages=2, use_sitemaps=False, topic_keywords=["survival"])
    )
    assert [p.url for p in guided.pages][1] == "https://army.mil/training/field-survival"
    assert guided.documents["https://army.mil/files/FM-21-76.pdf"].title == "Survival Field Manual"


def test_sitemap_pages_on_a_topic_are_crawled():
    sitemap = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://army.mil/article/1/unit-news</loc></url>
      <url><loc>https://army.mil/article/2/cold-weather-survival</loc></url>
    </urlset>"""
    pages = {
        **ARMY,
        "https://army.mil/sitemap.xml": (200, "application/xml", sitemap),
        "https://army.mil/article/2/cold-weather-survival": (200, "text/html", html("Cold weather survival")),
    }
    fetcher = FakeFetcher(pages)
    crawl_site("army.mil", fetcher, CrawlLimits(max_pages=3, topic_keywords=["survival"]))
    assert "https://army.mil/article/2/cold-weather-survival" in fetcher.requested
    assert "https://army.mil/article/1/unit-news" not in fetcher.requested


def test_guess_owner():
    assert guess_owner("army.mil") == ("federal", None)
    assert guess_owner("fs.fed.us") == ("federal", None)
    assert guess_owner("dot.state.tx.us") == ("state", "TX")
    assert guess_owner("example.org") == ("other", None)


def test_agency_directory_names_a_site_added_by_hand(tmp_path):
    conn = db.connect(str(tmp_path / "w.db"))
    db.sync_sites(conn, parse_registry(REGISTRY_CSV))
    db.add_site(conn, "army.mil", organization="army.mil", level="federal")
    conn.execute("UPDATE sites SET crawled_at = '2026-09-01' WHERE domain = 'army.mil'")
    db.add_subsites(conn, db.get_site(conn, "army.mil"), ["history.army.mil"])
    army = {"id": 30, "name": "Army Department", "short_name": "Army", "parent_id": None,
            "agency_url": "https://www.army.mil/"}  # fmt: skip
    stats = import_agencies(conn, parse_agencies([army]))
    site = db.get_site(conn, "army.mil")
    assert (stats.named_sites, stats.new_sites) == (1, 0)
    assert (site["organization"], site["source"], site["crawled_at"]) == ("Army Department", "agency", "2026-09-01")
    assert db.get_site(conn, "history.army.mil")["organization"] == "Army Department"


@pytest.fixture
def army_cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "registry.csv").write_text(REGISTRY_CSV)
    (tmp_path / "govbooks.toml").write_text(CONFIG)
    fetcher = FakeFetcher(ARMY)
    monkeypatch.setattr(cli, "make_fetcher", lambda args: fetcher)
    cli.main(["--db", "w.db", "sync", "--file", "registry.csv"])
    return fetcher


def run(capsys, *argv):
    code = cli.main(["--db", "w.db", *argv])
    return code, capsys.readouterr().out


def test_cli_crawl_with_sub_sites_and_topics(army_cli, capsys):
    code, out = run(capsys, "crawl", "https://www.army.mil/", "--with-subsites", "--max-subsites", "1",
                    "--all-topics", "--no-sitemaps")  # fmt: skip
    assert code == 0
    assert "army.mil is not on the map yet (federal by its domain); added it" in out
    assert "Following links about: survival, first aid, beekeeping, honey bee" in out
    # history.army.mil sounds like a publisher, so it goes before home.army.mil.
    assert "Sub-sites: history.army.mil" in out
    assert "https://home.army.mil/" not in army_cli.requested
    assert db.get_site(db.connect("w.db"), "history.army.mil")["level"] == "federal"

    code, out = run(capsys, "crawl", "army.mil", "--with-subsites", "--no-sitemaps")
    assert "Sub-sites: home.army.mil" in out  # the next one; history.army.mil was crawled recently


def test_cli_export_topics_column_and_summary(army_cli, capsys):
    run(capsys, "crawl", "army.mil", "--with-subsites", "--no-sitemaps")
    assert run(capsys, "export", "--out", "all.csv")[0] == 0
    with open("all.csv", newline="") as fh:
        rows = {r["title"]: r for r in csv.DictReader(fh)}
    assert rows["Survival Field Manual"]["topics"] == "survival"
    assert rows["Beekeeping at Fort Bragg: A History"]["topics"] == "beekeeping"
    assert rows["Visitor Guide"]["topics"] == ""

    code, out = run(capsys, "export", "--out", "topics.csv", "--topics-only")
    assert "Wrote 2 row(s)" in out
    code, out = run(capsys, "export", "--out", "summary.md", "--heading", "Found this week")
    text = open("summary.md").read()
    assert text.startswith("## Found this week\n\n3 book(s).")
    lines = [line for line in text.splitlines() if line.startswith("| [")]
    assert "Visitor Guide" in lines[-1]  # topic books first
    assert "[Survival Field Manual](https://army.mil/files/FM-21-76.pdf) | survival |" in text


def test_cli_amazon_checks_topic_books_first(army_cli, capsys, monkeypatch):
    run(capsys, "crawl", "army.mil", "--with-subsites", "--no-sitemaps")
    provider = FakeProvider({})
    monkeypatch.setattr("govbooks.market.build_provider", lambda name, http, config: provider)
    assert run(capsys, "amazon", "--limit", "2")[0] == 0
    assert sorted(provider.queries) == ["Beekeeping at Fort Bragg", "Survival Field Manual"]
    code, out = run(capsys, "amazon", "--topics-only")
    assert "Nothing to check" in out


def test_a_site_that_redirects_to_another_mapped_site_is_not_crawled_twice(army_cli, capsys):
    army_cli.pages["https://presidiotrust.gov/"] = (301, "text/html", "https://www.presidio.gov/")
    army_cli.pages["https://www.presidio.gov/"] = (200, "text/html", html("Presidio", ("/a.pdf", "Trail Guide")))
    conn = db.connect("w.db")
    for host in ("presidio.gov", "presidiotrust.gov"):
        db.add_site(conn, host, organization="Presidio Trust", level="federal")
    code, out = run(capsys, "crawl", "presidiotrust.gov", "--no-sitemaps")
    assert "presidiotrust.gov: same website as www.presidio.gov, not crawled twice" in out
    assert db.get_site(conn, "presidiotrust.gov")["crawl_status"] == "alias"
    assert db.select_documents(conn) == []
    code, out = run(capsys, "crawl", "presidio.gov", "--no-sitemaps")
    assert "presidio.gov: 1 pages (0 publication pages), 1 documents" in out


def test_page_families_that_link_to_no_documents_are_given_up():
    from govweb.crawl import PATTERN_TRIES, page_pattern

    assert page_pattern("https://a.mil/PubForm/Series_Details.aspx?SERIES_ID=15") == "/pubform/series_details.aspx?SERIES_ID"
    assert page_pattern("https://a.mil/article/286911") == "/article/#"
    series = [(f"/series?id={i}", f"Series {i}") for i in range(12)]
    pages = {
        "https://a.mil/": (200, "text/html", html("Home", *series, ("/pubs/list", "Publications"))),
        "https://a.mil/pubs/list": (200, "text/html", html("Pubs", ("/files/field-manual.pdf", "Field Manual"))),
        **{f"https://a.mil/series?id={i}": (200, "text/html", html(f"Series {i}")) for i in range(12)},
    }
    fetcher = FakeFetcher(pages)
    result = crawl_site("a.mil", fetcher, CrawlLimits(use_sitemaps=False))
    assert sum("/series?id=" in u for u in fetcher.requested) == PATTERN_TRIES
    assert "https://a.mil/files/field-manual.pdf" in result.documents


def test_a_site_stops_at_its_time_limit():
    result = crawl_site("army.mil", FakeFetcher(ARMY), CrawlLimits(use_sitemaps=False, max_seconds=1e-9))
    assert [p.url for p in result.pages] == ["https://army.mil/"]
    assert result.error.startswith("stopped after")


def test_titles_are_cleaned():
    from govweb.classify import best_title
    from govweb.parse import parse_html

    url = "https://a.mil/x.pdf"
    assert best_title("K9H2F Handbook 2026 K9H2F Handbook 2026", url) == "K9H2F Handbook 2026"
    assert best_title("Don&#39;t be a Passive Bystander handbook", url) == "Don't be a Passive Bystander handbook"
    assert best_title("1935 Government Organization Manual.", url) == "1935 Government Organization Manual"
    links = parse_html(
        '<a href="/a.pdf" title="Army.mil Style Guide">Army.mil Style Guide</a>'
        '<a href="/b.pdf" title="Weapon Systems Handbook 2020">Handbook</a>'
        '<a href="/c.pdf" title="Download">Bee book</a>',
        "https://a.mil/",
    ).links
    assert [link.text for link in links] == ["Army.mil Style Guide", "Weapon Systems Handbook 2020", "Download Bee book"]


def test_an_alias_of_an_unreachable_site_is_crawled(army_cli, capsys):
    army_cli.pages["https://federalcourts.gov/"] = (301, "text/html", "https://www.uscourts.gov/")
    army_cli.pages["https://www.uscourts.gov/"] = (200, "text/html", html("Courts", ("/a.pdf", "Jury Handbook")))
    conn = db.connect("w.db")
    for host in ("uscourts.gov", "federalcourts.gov"):
        db.add_site(conn, host, organization="US Courts", level="federal")
    conn.execute("UPDATE sites SET crawl_status = 'unreachable' WHERE domain = 'uscourts.gov'")
    conn.commit()
    code, out = run(capsys, "crawl", "federalcourts.gov", "--no-sitemaps")
    assert "federalcourts.gov: 1 pages (0 publication pages), 1 documents" in out


def test_cli_crawls_only_the_sub_sites(army_cli, capsys):
    run(capsys, "crawl", "army.mil", "--no-sitemaps")
    army_cli.requested.clear()
    code, out = run(capsys, "crawl", "army.mil", "--subsites-only", "--no-sitemaps", "--max-minutes", "5")
    assert "Sub-sites: history.army.mil, home.army.mil" in out
    assert "https://army.mil/" not in army_cli.requested


def test_stored_titles_are_cleaned_once(tmp_path):
    path = tmp_path / "old.db"
    conn = db.connect(str(path))
    db.add_site(conn, "army.mil", organization="Army", level="federal")
    conn.execute(
        "INSERT INTO documents (url, domain, title, file_type, first_seen, last_seen) "
        "VALUES ('https://army.mil/k.pdf', 'army.mil', 'K9H2F Handbook K9H2F Handbook', 'pdf', 'x', 'x')"
    )
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    conn = db.connect(str(path))
    doc = conn.execute("SELECT title, book_score FROM documents").fetchone()
    assert (doc["title"], doc["book_score"]) == ("K9H2F Handbook", 2)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.DATA_VERSION


def test_cli_state_crawl_puts_publisher_sites_first(army_cli, capsys):
    conn = db.connect("w.db")
    for host in ("aaa.ca.gov", "archives.ca.gov", "zzz.ca.gov"):
        db.add_site(conn, host, organization=host, level="state", state="CA")
        army_cli.pages[f"https://{host}/"] = (200, "text/html", html(host))
    code, out = run(capsys, "crawl", "--level", "state", "--state", "CA", "--limit", "1", "--no-sitemaps")
    assert code == 0 and "archives.ca.gov" in out and "aaa.ca.gov" not in out
    run(capsys, "crawl", "--level", "state", "--state", "CA", "--no-sitemaps")  # the rest, ca.gov included
    code, out = run(capsys, "crawl", "--level", "state", "--state", "CA", "--no-sitemaps")
    assert (code, out.strip()) == (0, "Nothing to crawl: every chosen site was crawled recently.")
