"""End-to-end runs of the command line against canned API responses."""

import pytest

from govbooks import cli
from tests.conftest import FEDERAL_REGISTER, FakeHttp

CONFIG = """
database = "test.db"

[sources]
enabled = ["internet_archive", "open_library"]
max_results_per_query = 20

[amazon]
provider = "catalog"

[topics.beekeeping]
keywords = ["beekeeping"]
"""


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    (tmp_path / "govbooks.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    http = FakeHttp()
    http.add("GET", "federalregister.gov", FEDERAL_REGISTER)
    http.add("GET", "archive.org/advancedsearch.php", {"response": {"numFound": 1, "docs": [
        {"identifier": "beekeep1943", "title": "Beekeeping for beginners", "year": "1943",
         "creator": "United States. Department of Agriculture"},
    ]}})  # fmt: skip
    http.add("GET", "openlibrary.org/search.json", lambda url, params, headers: (
        {"numFound": 0, "docs": []} if "q" in params else  # discovery
        {"docs": [{"title": "Beekeeping for beginners", "isbn": ["9780306406157"]}]}  # market check
    ))  # fmt: skip
    http.add("GET", "googleapis.com/books", {"items": []})
    monkeypatch.setattr(cli, "make_http", lambda: http)
    return tmp_path


def run(capsys, *argv):
    code = cli.main(list(argv))
    return code, capsys.readouterr().out


def test_full_pipeline(workdir, capsys):
    code, out = run(capsys, "agencies", "sync", "--federal-only")
    assert code == 0 and "3 federal agencies" in out

    code, out = run(capsys, "agencies", "tree")
    assert "  - Agriculture Department (USDA)  [fr:12]" in out

    code, out = run(capsys, "discover", "beekeeping")
    assert code == 0 and "1 new book(s)" in out

    code, out = run(capsys, "books", "list")
    assert "Beekeeping for beginners" in out and "Agriculture Department" in out
    book_id = out.splitlines()[2].split()[0]

    code, out = run(capsys, "market", "check", "--topic", "beekeeping")
    assert code == 0 and "some_competition, 1 matching listing(s)" in out

    code, out = run(capsys, "books", "show", book_id)
    assert "0306406152" in out and "likely_public_domain" in out

    code, out = run(capsys, "track", "add", "--book", book_id)
    code, out = run(capsys, "track", "run")
    assert "first snapshot: 1 listing(s)" in out

    code, out = run(capsys, "report", "--out", "reports/bees.md")
    report = (workdir / "reports" / "bees.md").read_text()
    assert "Beekeeping for beginners" in report and "some_competition" in report
    code, out = run(capsys, "report", "--out", "bees.csv")
    assert (workdir / "bees.csv").read_text().startswith("id,title,year,agency")


def test_init_writes_example_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code, _ = run(capsys, "init")
    assert code == 0
    text = (tmp_path / "govbooks.toml").read_text()
    assert "[topics.beekeeping]" in text
    assert run(capsys, "init")[0] == 1  # refuses to overwrite


def test_unknown_topic_is_a_clear_error(workdir, capsys):
    with pytest.raises(SystemExit, match="Unknown topic 'bees'"):
        cli.main(["discover", "bees"])


def test_keepa_without_key_is_a_clear_error(workdir, monkeypatch):
    monkeypatch.delenv("KEEPA_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="KEEPA_API_KEY"):
        cli.main(["track", "add", "--asin", "B01"])
        cli.main(["track", "run", "--provider", "keepa"])


def test_discover_by_unmapped_state_is_an_error(workdir, capsys):
    run(capsys, "agencies", "sync", "--federal-only")
    with pytest.raises(SystemExit, match="agencies sync --state"):
        cli.main(["discover", "beekeeping", "--state", "OH"])
    with pytest.raises(SystemExit, match="Unknown agency id"):
        cli.main(["discover", "beekeeping", "--under", "fr:nope"])
