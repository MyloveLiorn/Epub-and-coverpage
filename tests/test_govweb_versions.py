"""Watching found books for new versions (govweb.versions) and the track-books command."""

from __future__ import annotations

from govweb import cli, versions
from govweb.export import SHEET_COLUMNS, hyperlink
from tests.test_govweb import FakeFetcher, html


def test_editions_are_told_apart_and_compared():
    assert versions.edition_of("FFY 2026 Final Refugee Resettlement Program State Plan") == (2026, 0)
    assert versions.edition_of("WV FY26 Refugee State Plan") == (2026, 0)
    assert versions.edition_of("Guide to Immigrant Eligibility for Federal Programs, Fourth Edition") == (0, 4)
    assert versions.edition_of("Practice Manual, 5th edition") == (0, 5)
    assert versions.edition_of("Practice Manual v2.1") == (0, 2.1)
    assert versions.edition_key("FFY 2026 Final Refugee Resettlement Program State Plan") == (
        versions.edition_key("FFY 2027 Refugee Resettlement Program State Plan (Revised July 2027)")
    )
    assert versions.edition_key("Chapter 12 Asylum") != versions.edition_key("Chapter 13 Asylum")
    page = [("https://x.gov/plan-2027.pdf", "FFY 2027 Refugee Resettlement Program State Plan"),
            ("https://x.gov/plan-2025.pdf", "FFY 2025 Refugee Resettlement Program State Plan"),
            ("https://x.gov/other.pdf", "FFY 2028 Refugee Health Guide")]  # fmt: skip
    assert versions.newer_edition("FFY 2026 Final Refugee Resettlement Program State Plan", page) == page[0]
    assert versions.newer_edition("FFY 2027 Refugee Resettlement Program State Plan", page) is None
    assert versions.newer_edition("Report", [("https://x.gov/r.pdf", "Report 2030")]) is None  # too vague


def test_hyperlinks_are_read_back():
    cell = hyperlink("https://a.gov/x.pdf", 'The "X" Guide')
    assert versions.parse_hyperlink(cell) == ("https://a.gov/x.pdf", 'The "X" Guide')
    assert versions.parse_hyperlink("plain text") == ("", "plain text")


def search_tab(*books):
    rows = [SHEET_COLUMNS]
    for link, title, pages, found_on in books:
        row = dict.fromkeys(SHEET_COLUMNS, "")
        row.update({"Title (link)": hyperlink(link, title), "Pages": pages, "Found on (page)": found_on})
        rows.append([row[c] for c in SHEET_COLUMNS])
    return rows


def test_books_are_tracked_checked_and_new_versions_found():
    today = "2026-10-01"
    found = versions.from_search_tab(search_tab(
        ("https://x.gov/plan-2026.pdf", "FFY 2026 Refugee State Plan", "86", "https://x.gov/plans"),
        ("https://x.gov/flyer.pdf", "Refugee Flyer", "2", "https://x.gov/plans"),  # too short to track
        ("https://x.gov/manual.pdf", "Refugee Health Manual", "85", "https://x.gov/sitemap.xml"),
    ))  # fmt: skip
    assert [b.title for b in found] == ["FFY 2026 Refugee State Plan", "Refugee Health Manual"]
    texas = search_tab(("https://tx.gov/guide.pdf", "Texas Immigration Guide", "40", ""))
    texas[1][SHEET_COLUMNS.index("State")] = "TX"  # its publications can't be reused
    assert versions.from_search_tab(texas) == []
    # A book added by hand: a title and the page it is published on.
    row = dict.fromkeys(versions.COLUMNS, "")
    row.update({"Title (link)": "Welcome Guide", "Found on (page)": "https://x.gov/welcome", "Notes": "mine"})
    hand = versions.from_tracked_tab([versions.COLUMNS, [row[c] for c in versions.COLUMNS]])
    assert (hand[0].link, hand[0].found_on, hand[0].notes, hand[0].added_by) == (
        "", "https://x.gov/welcome", "mine", versions.YOU)
    books = versions.merge(hand, found, today)
    assert len(versions.merge(books, found, today)) == 3  # tracked once

    site = {
        "https://x.gov/plan-2026.pdf": (200, "application/pdf", b"%PDF-1.4 old"),
        "https://x.gov/manual.pdf": (200, "application/pdf", b"%PDF-1.4 manual"),
        "https://x.gov/plans": (200, "text/html", html("Plans", ("/plan-2026.pdf", "FFY 2026 Refugee State Plan"))),
        "https://x.gov/welcome": (200, "text/html", html("Welcome", ("/welcome-2020.pdf", "Welcome Guide 2020"))),
    }
    fetcher = FakeFetcher(site)
    for book in books:
        versions.check(book, fetcher, today)
    assert {b.title: b.status for b in books} == {"Welcome Guide": versions.TRACKING,
                                                  "FFY 2026 Refugee State Plan": versions.TRACKING,
                                                  "Refugee Health Manual": versions.TRACKING}  # fmt: skip
    # A week later: a new edition on the plans page, the manual replaced, the welcome guide's page moved on.
    site["https://x.gov/plans"] = (200, "text/html", html("Plans", ("/plan-2026.pdf", "FFY 2026 Refugee State Plan"),
                                                          ("/plan-2027.pdf", "FFY 2027 Refugee State Plan")))
    site["https://x.gov/manual.pdf"] = (200, "application/pdf", b"%PDF-1.4 new manual")
    rows = [versions.COLUMNS] + [b.row() for b in books]
    books = versions.from_tracked_tab(rows)  # as read back from the sheet
    for book in books:
        versions.check(book, fetcher, "2026-10-08")
    status = {b.title: (b.status, b.newer_link, b.changed_on) for b in books}
    assert status["FFY 2026 Refugee State Plan"] == (versions.NEWER, "https://x.gov/plan-2027.pdf", "2026-10-08")
    assert status["Refugee Health Manual"] == (versions.CHANGED, "", "2026-10-08")
    assert status["Welcome Guide"][0] == versions.NO_CHANGE  # "Welcome Guide 2020" was there from the start
    welcome = next(b for b in books if b.title == "Welcome Guide")
    site["https://x.gov/welcome"] = (200, "text/html", html("Welcome", ("/welcome-2020.pdf", "Welcome Guide 2020"),
                                                            ("/welcome-2024.pdf", "Welcome Guide (2024 edition)")))
    versions.check(welcome, fetcher, "2026-10-09")
    assert (welcome.status, welcome.newer_link) == (versions.NEWER, "https://x.gov/welcome-2024.pdf")
    assert sum(len(b.events) for b in books) == 3
    del site["https://x.gov/manual.pdf"]
    manual = next(b for b in books if b.title == "Refugee Health Manual")
    assert versions.check(manual, fetcher, "2026-10-15").status == versions.GONE
    manual.track = "no"
    assert not manual.active


class SheetSession:
    """A fake Google Sheets API holding tabs of cells."""

    def __init__(self, tabs):
        self.tabs = tabs

    def get(self, url, params=None, **kw):
        from urllib.parse import unquote

        from tests.test_govweb_results import FakeResponse

        if "/values/" in url:
            title = unquote(url.split("/values/")[1]).strip("'")
            return FakeResponse(body={"values": self.tabs.get(title, [])})
        return FakeResponse(body={"sheets": [{"properties": {"title": t}} for t in self.tabs]})

    def post(self, url, json=None, **kw):
        from tests.test_govweb_results import FakeResponse

        for request in (json or {}).get("requests", []):
            self.tabs[request["addSheet"]["properties"]["title"]] = []
        return FakeResponse()

    def put(self, url, json=None, **kw):
        from urllib.parse import unquote

        from tests.test_govweb_results import FakeResponse

        title = unquote(url.split("/values/")[1]).split("'!")[0].strip("'")
        self.tabs[title] = json["values"]
        return FakeResponse()


def test_track_books_command_keeps_the_tab(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    sheet = SheetSession({"Search - immigration": search_tab(
        ("https://x.gov/plan-2026.pdf", "FFY 2026 Refugee State Plan", "86", "https://x.gov/plans"))})
    monkeypatch.setattr("govweb.sheets.authorized_session", lambda credentials: sheet)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    fetcher = FakeFetcher({"https://x.gov/plan-2026.pdf": (200, "application/pdf", b"%PDF-1.4 old"),
                           "https://x.gov/plans": (200, "text/html", html("Plans"))})  # fmt: skip
    monkeypatch.setattr(cli, "make_fetcher", lambda args: fetcher)
    assert cli.main(["--db", "w.db", "track-books", "--sheet-id", "S", "--summary", "v.md"]) == 0
    tab = sheet.tabs[versions.TAB]
    assert tab[0] == versions.COLUMNS and len(tab) == 2
    assert tab[1][0].startswith('=HYPERLINK("https://x.gov/plan-2026.pdf"')
    assert tab[1][versions.COLUMNS.index("Status")] == versions.TRACKING
    assert "0 change(s)" in (tmp_path / "v.md").read_text()
    # The next run reads the tab back and keeps one row per book.
    assert cli.main(["--db", "w.db", "track-books", "--sheet-id", "S"]) == 0
    assert len(sheet.tabs[versions.TAB]) == 2
    assert sheet.tabs[versions.TAB][1][versions.COLUMNS.index("Status")] == versions.NO_CHANGE


def test_paperwork_isnt_tracked_and_search_rows_are_kept_up_to_date():
    today = "2026-10-01"
    found = versions.from_search_tab(search_tab(
        ("https://x.gov/nofo.pdf", "FY18NOFO Refugee and Asylee Grant Recipients", "50", ""),
        ("https://x.gov/remarks.pdf", "Director's Remarks at the Immigration Conference", "27", ""),
        ("https://x.gov/manual.pdf", "Refugee Health Manual", "85", ""),
    ))  # fmt: skip
    assert [b.title for b in found] == ["Refugee Health Manual"]
    # Rows tracked before the paperwork filter (no "Added by" column yet): paperwork is dropped
    # unless the user wrote about it; a better title from the search tab replaces the old one.
    old_columns = [c for c in versions.COLUMNS if c != "Added by"]

    def old_row(link, title, notes=""):
        row = dict.fromkeys(old_columns, "")
        row.update({"Title (link)": hyperlink(link, title), "Link": link, "Notes": notes, "Pages": "30"})
        return [row[c] for c in old_columns]

    tracked = versions.from_tracked_tab([old_columns, old_row("https://x.gov/foa.pdf", "FY11 Citizenship FOA"),
                                         old_row("https://x.gov/grants.pdf", "Grant Recipients", notes="keep"),
                                         old_row("https://x.gov/manual.pdf", "manual")])  # fmt: skip
    assert all(b.added_by == versions.SEARCH for b in tracked)
    merged = versions.merge(tracked, found, today)
    assert [(b.title, b.pages) for b in merged] == [("Grant Recipients", "30"), ("Refugee Health Manual", "85")]
    # Books added by hand stay whatever their title.
    row = dict.fromkeys(versions.COLUMNS, "")
    row.update({"Title (link)": "Refugee flyer", "Link": "https://x.gov/flyer.pdf"})
    mine = versions.from_tracked_tab([versions.COLUMNS, [row[c] for c in versions.COLUMNS]])
    assert [b.title for b in versions.merge(mine, [], today)] == ["Refugee flyer"]
