"""govweb: Amazon checks, the results table, and Google Sheets upload."""

import csv
import json

import pytest

from govbooks import copyright_policy
from govbooks.models import Listing
from govweb import cli, db
from govweb.amazon import check_documents
from govweb.crawl import CrawlLimits, crawl_site
from govweb.export import as_table, result_rows, safe_cell
from govweb.registry import parse_registry
from govweb.sheets import SheetsError, upload
from tests.test_govweb import REGISTRY_CSV, ROBOTS, SITE, FakeFetcher


class FakeProvider:
    name = "catalog"
    has_sales_rank = False

    def __init__(self, results):
        self.results = results
        self.queries = []

    def search(self, query, limit=10):
        self.queries.append(query)
        return self.results.get(query, [])

    def lookup(self, asins):
        return []


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setattr(copyright_policy, "state_policies", lambda: {})
    connection = db.connect(":memory:")
    db.sync_sites(connection, parse_registry(REGISTRY_CSV))
    db.store_result(connection, crawl_site("usda.gov", FakeFetcher(SITE, ROBOTS), CrawlLimits(max_pages=10)))
    yield connection
    connection.close()


def test_amazon_check_and_results_table(conn):
    provider = FakeProvider({
        "Complete Guide to Home Canning": [
            Listing("0306406152", "Complete Guide to Home Canning (Reprint)", authors=["U.S. Dept. of Agriculture"]),
            Listing("0486453413", "Complete Guide to Home Canning", authors=["Jane Smith"]),  # another book
        ],
        "Beekeeping in the United States": [Listing("B000000001", "Honey Recipes")],
    })  # fmt: skip
    docs = db.documents_to_check(conn, older_than=db.now(), limit=10)
    assert {d["title"] for d in docs} == {"Complete Guide to Home Canning", "Beekeeping in the United States"}
    assert check_documents(conn, provider, docs) == (2, 1)
    assert db.documents_to_check(conn, older_than="2000-01-01", limit=10) == []  # checked recently

    rows = {r["title"]: r for r in result_rows(db.export_rows(conn))}
    canning = rows["Complete Guide to Home Canning"]
    assert (canning["on_amazon"], canning["amazon_editions"]) == ("yes", 1)
    assert canning["amazon_link"] == "https://www.amazon.com/dp/0306406152"
    assert (canning["rights"], canning["publisher"], canning["new"]) == ("likely_public_domain", "Department of Agriculture", "")
    bees = rows["Beekeeping in the United States"]
    assert bees["on_amazon"] == "not found"  # the free check can miss Kindle-only listings
    assert bees["amazon_link"].startswith("https://www.amazon.com/s?k=Beekeeping")


def test_safe_cells_never_become_formulas():
    assert safe_cell('=HYPERLINK("http://evil")') == "'=HYPERLINK(\"http://evil\")"
    assert safe_cell("+1 guide") == "'+1 guide" and safe_cell("@home") == "'@home"
    assert safe_cell("-5") == "-5" and safe_cell("- list") == "'- list"
    assert safe_cell(None) == "" and safe_cell(3) == "3"
    table = as_table([{c: "=x" if c == "title" else "" for c in as_table([])[0]}])
    assert table[1][table[0].index("title")] == "'=x"


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = json.dumps(self._body)
        self.content = self.text.encode()

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, tabs, fail_on=None):
        self.tabs = tabs
        self.fail_on = fail_on
        self.calls = []

    def _record(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.fail_on and self.fail_on in url:
            return FakeResponse(403, {"error": "denied"})
        if method == "GET":
            return FakeResponse(body={"sheets": [{"properties": {"title": t}} for t in self.tabs]})
        return FakeResponse()

    def get(self, url, **kw):
        return self._record("GET", url, **kw)

    def post(self, url, **kw):
        return self._record("POST", url, **kw)

    def put(self, url, **kw):
        return self._record("PUT", url, **kw)


def test_sheet_upload_creates_tabs_and_writes_plain_text():
    session = FakeSession(["New books"])
    upload(session, "SHEET", {"New books": [["title"], ["A"]], "Website books": [["title"]]})
    methods = [(m, u.split("/SHEET")[1]) for m, u, _ in session.calls]
    assert methods[0] == ("GET", "")
    assert methods[1] == ("POST", ":batchUpdate")
    assert session.calls[1][2]["json"] == {"requests": [{"addSheet": {"properties": {"title": "Website books"}}}]}
    assert ("POST", "/values/%27New%20books%27:clear") in methods
    put = next(c for c in session.calls if c[0] == "PUT" and "New%20books" in c[1])
    assert put[2]["params"] == {"valueInputOption": "RAW"} and put[2]["json"] == {"values": [["title"], ["A"]]}


def test_sheet_upload_reports_errors():
    with pytest.raises(SheetsError, match="HTTP 403"):
        upload(FakeSession([], fail_on=":batchUpdate"), "SHEET", {"Tab": [["x"]]})


def test_cli_amazon_export_and_sheet(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(copyright_policy, "state_policies", lambda: {})
    (tmp_path / "registry.csv").write_text(REGISTRY_CSV)
    monkeypatch.setattr(cli, "make_fetcher", lambda args: FakeFetcher(SITE, ROBOTS))
    provider = FakeProvider({})
    monkeypatch.setattr("govbooks.market.build_provider", lambda name, http, config: provider)
    run = lambda *argv: cli.main(["--db", "w.db", *argv])  # noqa: E731
    run("sync", "--file", "registry.csv")
    run("crawl", "usda.gov")
    assert run("amazon") == 0
    assert "Checked 2: 0 already on Amazon, 2 not found." in capsys.readouterr().out
    assert run("export", "--out", "out/books.csv") == 0
    with open("out/books.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2 and rows[0]["on_amazon"] == "not found"

    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    assert run("sheet", "--sheet-id", "X", "--tab", "Books=out/books.csv") == 1
    session = FakeSession([])
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setattr("govweb.sheets.authorized_session", lambda credentials: session)
    assert run("sheet", "--sheet-id", "X", "--tab", "Books=out/books.csv", "--tab", "Missing=nope.csv") == 0
    out = capsys.readouterr().out
    assert "Skipping tab 'Missing'" in out and "Updated 1 tab(s): Books" in out
