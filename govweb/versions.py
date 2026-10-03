"""Watching the books found for new versions: the "Tracked books" tab of the Google Sheet.

Every book of 10 or more pages in the sheet's search tabs is tracked, and so is any book added to
the tab by hand: a row with its title and its PDF's address (Link), or the address of the page it
is published on (Found on), for books already sold on Amazon whose next edition should be
republished quickly. Each check:
- downloads the book again: different content at the same address is a new version, and a book
  that is gone may have been replaced;
- reads the page that linked to it again, looking for a newer edition of the same title
  ("FFY 2027 ... State Plan" after "FFY 2026 ...", "Fifth Edition" after "Fourth Edition").
Writing "no" in a book's Track column stops its checks.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from govweb.classify import best_title, book_score, clean_title, document_type, title_from_url
from govweb.exclude import excluded_states
from govweb.export import hyperlink, safe_cell
from govweb.parse import parse_html
from govweb.pdfpages import count_pages

TAB = "Tracked books"
SOURCE_TABS = ("Search - ", "States - ")  # the search tabs whose books are tracked
MIN_PAGES = 18
MAX_BYTES = 150_000_000
COLUMNS = ["Title (link)", "Link", "Authority", "State", "Source website", "Found on (page)", "Pages", "Track",
           "Status", "Newer version (link)", "Changed on", "Last checked", "Tracked since", "Added by", "Notes",
           "SHA-256", "Editions seen on the page"]  # fmt: skip
SEARCH, YOU = "search", "you"  # who added a book: a search tab, or the user by hand

NO_CHANGE, TRACKING, CHANGED, NEWER, GONE, UNREACHABLE = (
    "No change", "Tracking", "Changed at the same address", "Newer edition found", "Gone from the site",
    "Couldn't check",
)  # fmt: skip

_HYPERLINK = re.compile(r'^=HYPERLINK\("((?:[^"]|"")*)",\s*"((?:[^"]|"")*)"\)$', re.IGNORECASE)


def parse_hyperlink(cell: str) -> tuple[str, str]:
    """ '=HYPERLINK("https://a.gov/x.pdf", "The ""X"" Guide")' -> ("https://a.gov/x.pdf", 'The "X" Guide');
    a plain cell -> ("", cell)."""
    match = _HYPERLINK.match(cell.strip())
    if not match:
        return "", cell
    return match.group(1).replace('""', '"'), match.group(2).replace('""', '"')


# --- editions ------------------------------------------------------------------------

_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
             "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12}  # fmt: skip
_YEAR = re.compile(r"(?<![\d.])(1[89]\d\d|20\d\d)(?![\d.])")
_SHORT_FY = re.compile(r"\bf?fy\s?'?(\d{2})\b", re.IGNORECASE)  # FY26, FFY 25
_EDITION = re.compile(
    r"\b(?:(\d{1,2})(?:st|nd|rd|th)|(" + "|".join(_ORDINALS) + r"))\s+(?:edition|ed\b|revision|revised edition)"
    r"|\b(?:edition|version|revision|rev|v)\.?\s*(\d{1,2}(?:\.\d{1,2})?)\b",
    re.IGNORECASE,
)
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov",
           "dec")  # fmt: skip
_VERSION_WORDS = {"edition", "ed", "revised", "revision", "rev", "updated", "update", "version", "final", "draft",
                  "ffy", "fy", "fiscal", "year", "st", "nd", "rd", "th", *_ORDINALS, *_MONTHS}  # fmt: skip


def edition_of(title: str) -> tuple[int, float]:
    """(year, edition) a title names: "FFY 2026 State Plan" -> (2026, 0), "Guide, Fourth Edition" ->
    (0, 4), "FY26 Report v2" -> (2026, 2). Bigger is newer."""
    years = [int(y) for y in _YEAR.findall(title)] + [2000 + int(y) for y in _SHORT_FY.findall(title)]
    editions = []
    for number, word, version in _EDITION.findall(title):
        value = number or version or str(_ORDINALS.get(word.lower(), 0))
        try:
            editions.append(float(value))
        except ValueError:
            pass
    return max(years, default=0), max(editions, default=0.0)


_DATE = re.compile(r"\b(?:" + "|".join(_MONTHS) + r")\.?\s+\d{1,2}(?:st|nd|rd|th)?\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
                   re.IGNORECASE)  # fmt: skip


def edition_key(title: str) -> str:
    """The title without what changes from one edition to the next (years, dates, "FY26", "Fourth
    Edition", "revised"): two editions of a book share it; chapters 12 and 13 don't."""
    text = _DATE.sub(" ", title.lower())
    text = _SHORT_FY.sub(" ", _YEAR.sub(" ", text))
    text = _EDITION.sub(" ", text)
    return " ".join(w for w in re.findall(r"[a-z0-9]+", text) if w not in _VERSION_WORDS)


def same_book(title: str, candidates: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The (url, title) candidates that are editions of the book called ``title``."""
    key = edition_key(title)
    if len(key.split()) < 2:  # "Report" alone would match anything
        return []
    return [(url, t) for url, t in candidates if edition_key(t) == key]


def newer_edition(title: str, candidates: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Of (url, title) candidates, the newest edition of the same book that is newer than ``title``."""
    edition = edition_of(title)
    newer = [(edition_of(t), url, t) for url, t in same_book(title, candidates) if edition_of(t) > edition]
    if not newer:
        return None
    _, url, found = max(newer)
    return url, found


# --- the tracked list ------------------------------------------------------------------


@dataclass
class Tracked:
    link: str
    title: str
    authority: str = ""
    state: str = ""
    website: str = ""
    found_on: str = ""
    pages: str = ""
    track: str = "yes"
    status: str = ""
    newer_link: str = ""
    newer_title: str = ""
    changed_on: str = ""
    last_checked: str = ""
    tracked_since: str = ""
    added_by: str = SEARCH
    notes: str = ""  # the user's own
    sha256: str = ""
    seen: str = ""  # addresses of the editions of this book seen on its page, space-separated
    events: list[str] = field(default_factory=list)  # what this check found, for the run's summary

    @property
    def active(self) -> bool:
        return self.track.strip().lower() not in ("no", "stop", "n")

    @property
    def key(self) -> str:
        """One row per book: its address, or its title on the page it is published on."""
        return self.link or f"{self.found_on}#{self.title.lower()}"

    def row(self) -> list[str]:
        title = hyperlink(self.link or self.found_on, self.title) if (self.link or self.found_on) else safe_cell(
            self.title)
        newer = hyperlink(self.newer_link, self.newer_title or self.newer_link) if self.newer_link else ""
        return [title, *(safe_cell(v) for v in (self.link, self.authority, self.state, self.website, self.found_on,
                                                self.pages, self.track, self.status)),
                newer, *(safe_cell(v) for v in (self.changed_on, self.last_checked, self.tracked_since, self.added_by,
                                                self.notes, self.sha256, self.seen))]  # fmt: skip

    @property
    def paperwork(self) -> bool:
        return book_score(self.title, self.link or self.found_on) <= 0

    @property
    def untouched(self) -> bool:
        """Added by a search, with nothing of the user's or of its checks worth keeping."""
        return self.added_by == SEARCH and not (self.notes or self.newer_link or self.changed_on) and self.active


def _table(rows: list[list[str]]) -> list[dict[str, str]]:
    if not rows:
        return []
    header = rows[0]
    return [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in rows[1:]]


def _url(value: str) -> str:
    value = value.strip()
    return value if re.match(r"https?://\S+$", value) else ""


def from_tracked_tab(rows: list[list[str]]) -> list[Tracked]:
    """The books already tracked, as read back from the tab (formulas as written), and those added
    by hand: a title with a Link (the PDF) or a Found on page."""
    books = []
    by_hand = bool(rows) and "Added by" in rows[0]  # before the column, every row came from a search
    for r in _table(rows):
        title_link, title = parse_hyperlink(r.get("Title (link)", ""))
        found_on = _url(r.get("Found on (page)", ""))
        link = _url(r.get("Link", "")) or (title_link if title_link != found_on else "") or _url(title)
        if _url(title):  # a bare address typed as the title
            title = title_from_url(title)
        newer_link, newer_title = parse_hyperlink(r.get("Newer version (link)", ""))
        if link or found_on:
            books.append(Tracked(link, title.strip(), r.get("Authority", ""), r.get("State", ""),
                                 r.get("Source website", ""), found_on, r.get("Pages", ""), r.get("Track", "") or "yes",
                                 r.get("Status", ""), newer_link, newer_title, r.get("Changed on", ""),
                                 r.get("Last checked", ""), r.get("Tracked since", ""),
                                 (r.get("Added by", "").strip().lower() or YOU) if by_hand else SEARCH,
                                 r.get("Notes", ""), r.get("SHA-256", ""),
                                 r.get("Editions seen on the page", "")))  # fmt: skip
    return books


def from_search_tab(rows: list[list[str]], min_pages: int = MIN_PAGES) -> list[Tracked]:
    """The books of a search tab worth tracking: those of ``min_pages`` pages or more that aren't
    paperwork (grant notices, speeches, lists; govweb.classify.book_score)."""
    books = []
    for r in _table(rows):
        link, title = parse_hyperlink(r.get("Title (link)", ""))
        title = clean_title(title) or title
        if book_score(title, link) <= 0:
            continue
        pages = r.get("Pages", "")
        if r.get("State", "").strip().upper() in excluded_states():  # its publications can't be reused
            continue
        if link and pages.isdigit() and int(pages) >= min_pages:
            books.append(Tracked(link, title, r.get("Authority", ""), r.get("State", ""), r.get("Source website", ""),
                                 r.get("Found on (page)", ""), pages))  # fmt: skip
    return books


def from_watchlist(rows: list[list[str]]) -> list[Tracked]:
    """Books listed by hand in a file of the repository (automation/watchlist.csv): a Title with a
    Link (the PDF) and/or a Found on (page). They are tracked like books added to the tab by hand."""
    books = []
    for r in _table(rows):
        title, link, found_on = r.get("Title", "").strip(), _url(r.get("Link", "")), _url(r.get("Found on (page)", ""))
        if title and (link or found_on):
            books.append(Tracked(link, title, r.get("Authority", ""), "", r.get("Source website", ""), found_on,
                                 r.get("Pages", ""), added_by=YOU, notes=r.get("Notes", "")))  # fmt: skip
    return books


def merge(tracked: list[Tracked], found: list[Tracked], today: str) -> list[Tracked]:
    """The tracked books, plus the found ones not tracked yet (one row per book). A book a search
    added is brought up to date from the search tabs (title, pages, where it was found), and left
    out when it is paperwork the user hasn't written about and no check found a change for."""
    fresh = {book.key: book for book in found}
    merged: list[Tracked] = []
    known: set[str] = set()
    for book in tracked:
        if book.key in known:
            continue
        newer = fresh.get(book.key)
        if newer and book.added_by == SEARCH:
            book.title, book.pages = newer.title or book.title, newer.pages or book.pages
            book.authority, book.state = newer.authority or book.authority, newer.state or book.state
            book.website, book.found_on = newer.website or book.website, newer.found_on or book.found_on
        if book.untouched and book.paperwork:
            continue
        known.add(book.key)
        merged.append(book)
    for book in found:
        if book.key not in known:
            known.add(book.key)
            book.tracked_since = book.tracked_since or today
            merged.append(book)
    return merged


# --- checking ----------------------------------------------------------------------------


def check(book: Tracked, fetcher, today: str) -> Tracked:
    """Look at a book again: its file, and the page that linked to it."""
    first_check = not book.last_checked
    book.last_checked = today
    status = TRACKING if not (book.sha256 or book.status) else NO_CHANGE
    if book.link and not fetcher.allowed(book.link):
        book.status = f"{UNREACHABLE} (robots.txt)"
        return book
    fetched = fetcher.get(book.link, max_bytes=MAX_BYTES) if book.link else None
    if fetched is None:  # a book added with its page only: the page is watched
        pass
    elif fetched.status in (404, 410):
        status = GONE
        book.events.append(f"{GONE}: {book.title}")
    elif not fetched.ok:
        status = f"{UNREACHABLE} ({fetched.error or f'HTTP {fetched.status}'})"[:120]
    else:
        digest = hashlib.sha256(fetched.body).hexdigest()
        if book.sha256 and digest != book.sha256:
            status = CHANGED
            book.changed_on = today
            book.events.append(f"{CHANGED}: {book.title}")
            pages = count_pages(fetched.body)
            if pages:
                book.pages = str(pages)
        book.sha256 = digest
    newer = _newer_on_page(book, fetcher, first_check)
    if newer and newer[0] != book.newer_link:
        book.newer_link, book.newer_title = newer
        book.changed_on = today
        book.events.append(f"{NEWER}: {book.title} -> {newer[1]}")
    if book.newer_link and status in (NO_CHANGE, TRACKING):
        status = NEWER
    book.status = status
    return book


def _newer_on_page(book: Tracked, fetcher, first_check: bool) -> tuple[str, str] | None:
    """A newer edition of the book on the page that linked to it. The first look records the
    editions already there; later looks report an edition that wasn't (for a title without a year
    or edition number, any new one), and on the first look only an edition that is plainly newer."""
    page_url = book.found_on
    if not page_url.startswith("http") or page_url.endswith(".xml") or "sitemap" in page_url.lower():
        return None
    if not fetcher.allowed(page_url):
        return None
    fetched = fetcher.get(page_url, html_only=True)
    if not fetched.ok or not fetched.body:
        return None
    page = parse_html(fetched.text(), fetched.url)
    candidates = [(link.url, best_title(link.text, link.url, page.title)) for link in page.links
                  if document_type(link.url) and link.url != book.link]  # fmt: skip
    title = book.title or title_from_url(book.link)
    editions = same_book(title, candidates)
    seen = set(book.seen.split())
    book.seen = " ".join(sorted(seen | {url for url, _ in editions}))[:40_000]
    if first_check:  # a title without a year or edition can't be compared with what is there
        return newer_edition(title, editions) if edition_of(title) != (0, 0.0) else None
    fresh = [(url, t) for url, t in editions if url not in seen]
    if edition_of(title) == (0, 0.0):
        return max(fresh, key=lambda e: edition_of(e[1]), default=None)
    return newer_edition(title, fresh)


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
