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
