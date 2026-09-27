"""govweb: the .gov registry, page parsing, crawling, storage and the command line."""

import gzip
import urllib.robotparser

import pytest

from govweb import cli, db
from govweb.classify import best_title, document_type, hint_score, title_from_url
from govweb.crawl import CrawlLimits, crawl_site
from govweb.fetch import USER_AGENT, FetchResult
from govweb.parse import parse_html, parse_sitemap
from govweb.registry import level_of, parse_registry
from govweb.tree import build_tree, render

REGISTRY_CSV = """Domain name,Domain type,Organization name,Suborganization name,City,State,Security contact email
usda.gov,Federal - Executive,Department of Agriculture,,Washington,DC,cyber@usda.gov
recreation.gov,Federal - Executive,Department of Agriculture,Forest Service,Washington,DC,(blank)
ca.gov,State or territory,State of California,,Rancho Cordova,CA,(blank)
sos.ca.vote.gov,State or territory - Election,California Secretary of State,,Sacramento,CA,(blank)
29palms.gov,City,City of Twentynine Palms,,Twentynine Palms,CA,someone@example.com
"""


class FakeFetcher:
    """Serves a small fake website: {url: (status, content type, body)} plus robots.txt texts."""

    def __init__(self, pages: dict, robots: dict | None = None):
        self.pages = pages
        self.robots_txt = robots or {}
        self.requested: list[str] = []
        self._robots: dict = {}

    def get(self, url, html_only=False, max_bytes=None):
        self.requested.append(url)
        if url not in self.pages:
            return FetchResult(url=url, status=404, content_type="text/html")
        status, content_type, body = self.pages[url]
        if isinstance(body, str):
            body = body.encode()
        final_url = url
        if status in (301, 302):  # body holds the redirect target
            return self.get(body.decode(), html_only, max_bytes)
        if html_only and content_type != "text/html":
            body = b""
        return FetchResult(url=final_url, status=status, content_type=content_type, body=body)

    def robots(self, base_url):
        origin = "/".join(base_url.split("/")[:3])
        if origin not in self._robots:
            parser = urllib.robotparser.RobotFileParser(origin + "/robots.txt")
            parser.parse(self.robots_txt.get(origin, "").splitlines())
            self._robots[origin] = parser
        return self._robots[origin]

    def allowed(self, url):
        return self.robots(url).can_fetch(USER_AGENT, url)


def html(title, *links):
    anchors = "".join(f'<a href="{href}">{text}</a>' for href, text in links)
    return f"<html><head><title>{title}</title></head><body>{anchors}</body></html>"


SITEMAP = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.usda.gov/about</loc></url>
  <url><loc>https://www.usda.gov/media/publications</loc></url>
  <url><loc>https://www.usda.gov/files/Home_Canning-Guide_2015.pdf</loc></url>
</urlset>"""

SITE = {
    "https://usda.gov/": (301, "text/html", "https://www.usda.gov/"),
    "https://www.usda.gov/": (200, "text/html", html(
        "USDA",
        ("/about", "About"),
        ("/media/publications", "Publications"),
        ("/private/secret-reports", "Reports"),
        ("https://www.ars.usda.gov/research", "Research"),
        ("https://www.nal.usda.gov/files/beekeeping.pdf", "Beekeeping in the United States (PDF, 2 MB)"),
        ("https://www.epa.gov/other", "EPA"),
        ("mailto:info@usda.gov", "Mail"),
        ("/logo.png", "Logo"),
    )),  # fmt: skip
    "https://www.usda.gov/about": (200, "text/html", html("About", ("/about/history", "History"))),
    "https://www.usda.gov/media/publications": (200, "text/html", html(
        "Publications",
        ("/files/Home_Canning-Guide_2015.pdf", "Complete Guide to Home Canning"),
        ("/media/publications?page=2", "Next"),
        ("/get?id=77", "Download"),
    )),  # fmt: skip
    "https://www.usda.gov/media/publications?page=2": (200, "text/html", html("Publications 2")),
    "https://www.usda.gov/get?id=77": (200, "application/pdf", b"%PDF-1.4"),
    "https://www.usda.gov/sitemap.xml": (200, "application/xml", SITEMAP),
}
ROBOTS = {"https://www.usda.gov": "User-agent: *\nDisallow: /private/\n"}


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.sync_sites(connection, parse_registry(REGISTRY_CSV))
    yield connection
    connection.close()


def test_registry_levels_and_privacy():
    sites = {s.domain: s for s in parse_registry(REGISTRY_CSV)}
    assert (sites["usda.gov"].level, sites["usda.gov"].organization) == ("federal", "Department of Agriculture")
    assert sites["recreation.gov"].suborganization == "Forest Service"
    assert (sites["sos.ca.vote.gov"].level, sites["sos.ca.vote.gov"].election) == ("state", True)
    assert not any("example.com" in str(vars(s)) for s in sites.values())  # contact emails are dropped
    assert level_of("Special district") == ("special_district", False)
    assert level_of("Something new") == ("something_new", False)


def test_registry_old_header_format():
    old = "Domain Name,Domain Type,Agency,Organization,City,State\nNASA.GOV,Federal - Executive,NASA,Headquarters,DC,DC\n"
    site = parse_registry(old)[0]
    assert (site.domain, site.organization, site.suborganization) == ("nasa.gov", "NASA", "Headquarters")


def test_parse_html_links_titles_and_nofollow():
    page = parse_html(
        '<title> USDA \n Home</title><base href="https://x.gov/dir/">'
        '<a href="a.pdf"><img alt="Farm guide"></a><a href="#top">Top</a><a href="javascript:x()">J</a>'
        '<a href="/b#frag" title="Bee book">B</a>',
        "https://x.gov/",
    )
    assert page.title == "USDA Home"
    assert [(link.url, link.text) for link in page.links] == [
        ("https://x.gov/dir/a.pdf", "Farm guide"),
        ("https://x.gov/b", "Bee book B"),
    ]
    assert parse_html('<meta name="robots" content="noindex, nofollow">', "https://x.gov/").nofollow


def test_parse_sitemap_index_urlset_gzip_and_entities():
    index = b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://x.gov/s1.xml</loc></sitemap></sitemapindex>'
    assert parse_sitemap(index) == ([], ["https://x.gov/s1.xml"])
    urls, children = parse_sitemap(gzip.compress(SITEMAP))
    assert len(urls) == 3 and children == []
    assert parse_sitemap(b'<!DOCTYPE x [<!ENTITY a "b">]><urlset/>') == ([], [])
    assert parse_sitemap(b"not xml") == ([], [])


def test_classification():
    assert document_type("https://x.gov/a/B.PDF") == "pdf"
    assert document_type("https://x.gov/book.epub?download=1") == "epub"
    assert document_type("https://x.gov/page.html") is None
    assert hint_score("https://x.gov/media/publications") == 1
    assert hint_score("https://x.gov/about") == 0
    assert hint_score("https://x.gov/x", "Field guides and handbooks") >= 2
    assert title_from_url("https://x.gov/files/Home_Canning-Guide%202015.pdf") == "Home Canning Guide 2015"
    assert best_title("Download PDF", "https://x.gov/files/bee_keeping.pdf") == "bee keeping"
    assert best_title("Beekeeping in the United States (PDF, 2 MB)", "https://x.gov/b.pdf") == (
        "Beekeeping in the United States"
    )


def test_crawl_follows_redirect_robots_and_priorities():
    fetcher = FakeFetcher(SITE, ROBOTS)
    result = crawl_site("usda.gov", fetcher, CrawlLimits(max_pages=10))

    assert result.status == "ok"
    assert result.home_url == "https://www.usda.gov/"
    visited = [p.url for p in result.pages]
    assert "https://www.usda.gov/private/secret-reports" not in fetcher.requested  # robots.txt
    assert "https://www.epa.gov/other" not in fetcher.requested  # other domains
    assert "https://www.ars.usda.gov/research" not in fetcher.requested  # other hosts: noted, not crawled
    assert "https://www.usda.gov/logo.png" not in fetcher.requested
    # The publications page is crawled before the plain "About" page.
    assert visited.index("https://www.usda.gov/media/publications") < visited.index("https://www.usda.gov/about")
    assert result.other_hosts == {"ars.usda.gov": 1}
    docs = result.documents
    assert docs["https://www.usda.gov/files/Home_Canning-Guide_2015.pdf"].title == "Complete Guide to Home Canning"
    assert docs["https://www.nal.usda.gov/files/beekeeping.pdf"].title == "Beekeeping in the United States"
    assert docs["https://www.usda.gov/get?id=77"].file_type == "pdf"  # found by content type
    assert result.sitemap_urls == 3


def test_crawl_respects_page_budget_and_unreachable_sites():
    result = crawl_site("usda.gov", FakeFetcher(SITE, ROBOTS), CrawlLimits(max_pages=2, use_sitemaps=False))
    assert len(result.pages) == 2
    dead = crawl_site("nothing.gov", FakeFetcher({}), CrawlLimits())
    assert (dead.status, dead.pages) == ("unreachable", [])


def test_crawl_blocked_by_robots():
    fetcher = FakeFetcher(SITE, {"https://www.usda.gov": "User-agent: *\nDisallow: /\n"})
    assert crawl_site("usda.gov", fetcher).status == "blocked_by_robots"


def test_store_result_saves_documents_and_subsites(conn):
    result = crawl_site("usda.gov", FakeFetcher(SITE, ROBOTS), CrawlLimits(max_pages=10))
    assert db.store_result(conn, result) == 1  # ars.usda.gov

    site = db.select_sites(conn, domains=["usda.gov"])[0]
    assert (site["crawl_status"], site["documents_found"]) == ("ok", 3)
    sub = db.select_sites(conn, domains=["ars.usda.gov"])[0]
    assert (sub["parent_domain"], sub["organization"], sub["level"]) == ("usda.gov", "Department of Agriculture", "federal")
    docs = db.select_documents(conn, search="canning")
    assert [d["title"] for d in docs] == ["Complete Guide to Home Canning"]

    db.store_result(conn, result)  # re-crawling updates instead of duplicating
    assert len(db.select_documents(conn)) == 3


def test_map_tree(conn):
    lines = render(build_tree(db.select_sites(conn, include_election=True)))
    assert lines[0] == "federal (2 domains)"
    assert "  Department of Agriculture (2 domains)" in lines
    assert "      - recreation.gov" in lines
    assert "  CA (2 domains)" in lines  # state level is grouped by state first
    assert render(build_tree(db.select_sites(conn)), max_depth=0) == [
        "federal (2 domains)", "city (1 domains)", "state (1 domains)",
    ]  # fmt: skip


# --- command line -------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "registry.csv").write_text(REGISTRY_CSV)
    fetcher = FakeFetcher(
        {**SITE, "https://water.ca.gov/": (200, "text/html", html("DWR", ("/reports/bulletin-118.pdf", "Bulletin 118")))},
        ROBOTS,
    )  # fmt: skip
    monkeypatch.setattr(cli, "make_fetcher", lambda args: fetcher)
    return tmp_path


def run(capsys, *argv):
    code = cli.main(["--db", "web.db", *argv])
    return code, capsys.readouterr().out


def test_cli_end_to_end(cli_env, capsys):
    code, out = run(capsys, "sync", "--file", "registry.csv")
    assert code == 0 and "5 domains in the registry (5 new)" in out

    assert run(capsys, "crawl")[0] == 1  # refuses to crawl everything without being asked

    code, out = run(capsys, "crawl", "https://www.usda.gov/", "water.ca.gov", "--workers", "1")
    assert "usda.gov: " in out and "3 documents, 1 new sub-sites" in out
    assert "water.ca.gov: 1 pages (0 publication pages), 1 documents" in out

    code, out = run(capsys, "sites", "--state", "ca", "--level", "state")
    assert "water.ca.gov" in out and "State of California" in out

    code, out = run(capsys, "map", "--level", "federal")
    assert "- usda.gov  (3 documents)" in out and "- ars.usda.gov  [sub-site of usda.gov]" in out

    code, out = run(capsys, "docs", "--search", "canning")
    assert "Complete Guide to Home Canning" in out

    code, out = run(capsys, "stats")
    assert "federal" in out

    code, out = run(capsys, "docs", "--out", "docs.csv")
    assert (cli_env / "docs.csv").read_text().startswith("score,book_score,rights,title,file_type,url")

    code, out = run(capsys, "crawl", "--level", "federal")  # recently crawled sites are skipped
    assert "Crawling 2 site(s)" in out  # recreation.gov and the new ars.usda.gov sub-site


def test_cli_docs_by_govbooks_topic(cli_env, capsys):
    (cli_env / "govbooks.toml").write_text('[topics.canning]\nkeywords = ["home canning"]\n')
    run(capsys, "sync", "--file", "registry.csv")
    run(capsys, "crawl", "usda.gov")
    code, out = run(capsys, "docs", "--topic", "canning")
    lines = [line for line in out.splitlines() if "pdf" in line]
    assert len(lines) == 1 and "Complete Guide to Home Canning" in lines[0]
