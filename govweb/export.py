"""The results table: book-like documents with their topics, publisher, copyright screening and Amazon status."""

from __future__ import annotations

import csv
import re
import sqlite3
from collections.abc import Callable
from operator import itemgetter
from pathlib import Path
from urllib.parse import unquote, urlsplit

from govweb.amazon import amazon_link, amazon_status
from govweb.classify import split_language, title_from_url, title_key, url_year, year_in
from govweb.covers import LABELS as COVER_LABELS
from govweb.watch import Matcher, matching_topics, rights_of

# publisher: the authority (agency) that published it; website and found_on: where it was found.
COLUMNS = [
    "found", "new", "title", "year", "topics", "type", "pages", "cover", "versions", "publisher", "country", "level", "state", "website",
    "found_on", "link", "rights", "on_amazon", "amazon_editions", "amazon_best_rank", "amazon_link", "book_score",
    "rights_note",
]  # fmt: skip


def safe_cell(value: object) -> str:
    """Text from websites must not become a spreadsheet formula when the file is opened."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "@", "\t", "\r") or (text[:1] == "-" and not text[1:2].isdigit()):
        return "'" + text
    return text


# The .gov registry often names the office that runs a domain, not the one that publishes on it.
# state.gov is registered to its Bureau of Global Public Affairs, dhs.gov to the Management Directorate.
_IT_OFFICE = re.compile(r"chief information officer|information technology|\bocio\b|web services|digital services|"
                        r"public affairs|management directorate", re.IGNORECASE)  # fmt: skip


def publisher_of(row: sqlite3.Row) -> str:
    """The agency behind a document: the sub-organization, unless that is only the IT office."""
    sub = row["suborganization"]
    return sub if sub and not _IT_OFFICE.search(sub) else row["organization"]


def result_rows(
    rows: list[sqlite3.Row], marketplace: str = "www.amazon.com", topics: dict[str, Matcher] | None = None
) -> list[dict]:
    """One results row per document; ``topics`` names the topics each title matches."""
    out = []
    for row in rows:
        rights = rights_of(row)
        out.append(
            {
                "found": row["first_seen"][:10],
                "new": "yes" if row["first_crawled_at"] and row["first_seen"] > row["first_crawled_at"] else "",
                "title": row["title"],
                "year": publication_year(row),
                "topics": ", ".join(matching_topics(row, topics or {})),
                "type": row["file_type"],
                "pages": row["pages"] or "",
                "cover": COVER_LABELS.get(row["cover"] or "", "") if "cover" in row.keys() else "",
                "versions": 1,
                "publisher": publisher_of(row),
                "country": "United States",
                "level": row["level"],
                "state": row["state"] or "",
                "website": row["domain"],
                "link": row["url"],
                "rights": rights.status,
                "on_amazon": amazon_status(row),
                "amazon_editions": row["amazon_matching"] if row["amazon_matching"] is not None else "",
                "amazon_best_rank": row["amazon_best_rank"] or "",
                "amazon_link": amazon_link(row, marketplace),
                "book_score": row["book_score"],
                "found_on": row["found_on"] or "",
                "rights_note": rights.note,
            }
        )
    return out


def language_rank(title: str, url: str) -> int:
    """0: the title and file name name no language, 1: English, 2: another language."""
    languages = split_language(str(title))[1] + " " + split_language(title_from_url(url), file_name=True)[1]
    return 0 if not languages.strip() else 1 if re.fullmatch(r"(\s*(english|en|eng)\b)+\s*", languages) else 2


def _file_key(url: str) -> tuple[str, ...]:
    """The file's address without the language in its name: ".../guide-es.pdf" -> ".../guide"."""
    parts = urlsplit(url)
    folder = unquote(parts.path).rstrip("/").rsplit("/", 1)[0].lower()  # "a%20b" and "a b" are one address
    return ("file", parts.hostname or "", folder, split_language(title_from_url(url), file_name=True)[0].lower(),
            unquote(parts.query))  # fmt: skip


def variant_groups(items: list, title: Callable, url: Callable, site: Callable) -> list[list]:
    """Items grouped by book: the same title on a website, or the same file name, whatever
    language each is in. Groups keep the order of their first item."""
    groups: list[list] = []
    index: dict[tuple, int] = {}
    for item in items:
        keys = [_file_key(url(item)), ("title", site(item), title_key(str(title(item))) or str(title(item)).lower())]
        found = next((index[k] for k in keys if k in index), None)
        if found is None:
            found = len(groups)
            groups.append([])
        groups[found].append(item)
        for key in keys:
            index.setdefault(key, found)
    return groups


def pick_version(group: list, title: Callable, url: Callable) -> object:
    """The original of a book published in several languages: no language named, else English."""
    return min(group, key=lambda item: language_rank(title(item), url(item)))


def dedupe(rows: list[dict]) -> list[dict]:
    """One row per book per website: sites link the same book from two addresses, and publish
    it in many languages ("Asylum Guide - Spanish", "asylum-guide-es.pdf"); the original is kept,
    with the other versions' page count when its own is unknown."""
    out = []
    for group in variant_groups(rows, itemgetter("title"), itemgetter("link"), itemgetter("website")):
        row = dict(pick_version(group, itemgetter("title"), itemgetter("link")))
        if not row["pages"]:
            row["pages"] = max((r["pages"] for r in group if r["pages"]), default="")
        if not row.get("cover"):
            row["cover"] = next((r["cover"] for r in group if r.get("cover")), "")
        row["versions"] = len(group)
        out.append(row)
    return out


def publication_year(row: sqlite3.Row) -> int | str:
    """The year a document was published, as well as can be told: the year its title names, else
    the one its first page names or the PDF was made (count-pages), else the one in its address."""
    pdf_year = row["year"] if "year" in row.keys() else None
    return year_in(row["title"] or "") or pdf_year or url_year(row["url"]) or ""


def year_order(row: dict) -> tuple:
    """Sort key: topic books first and paperwork last; in between, the latest published first
    (those of unknown year last), the longest first within a year."""
    year = int(row["year"] or 0)
    return (not row["topics"], row["book_score"] < 0, year == 0, -year, -int(row["pages"] or 0))


def book_order(row: dict) -> tuple:
    """Sort key: topic books first and paperwork last; in between, the longest documents first
    (length is the best sign of a book), those of unknown length between long and short ones."""
    pages = int(row["pages"] or 0)
    length = 3 if pages >= 100 else 2 if pages >= 40 else 1 if pages >= 15 or not pages else 0
    return (not row["topics"], row["book_score"] < 0, -length, -row["book_score"], -pages)


# The Google Sheet layout: the title links to the book; where it came from, Amazon, pages, rights.
SHEET_COLUMNS = ["Search", "Title (link)", "Year", "Authority", "Country", "State", "Source website", "Found on (page)",
                 "On Amazon", "Amazon link", "Amazon sales rank", "Pages", "Cover page", "Copyright", "Topics",
                 "Found"]  # fmt: skip
RIGHTS_LABELS = {
    "public_domain": "Public domain", "likely_public_domain": "Likely public domain",
    "check": "Check the document", "likely_copyrighted": "Likely copyrighted", "unknown": "Unknown",
}  # fmt: skip


def hyperlink(url: str, text: str) -> str:
    """A HYPERLINK formula; quotes are doubled, so website text can't escape the string."""
    def literal(value: str) -> str:
        return '"' + " ".join(value.split()).replace('"', '""')[:1000] + '"'

    return f"=HYPERLINK({literal(url)}, {literal(text)})"


def sheet_table(rows: list[dict], search: str = "") -> list[list[str]]:
    """Header plus rows in the sheet layout. Only the title cell is a formula (the link); every
    other cell is made safe, so text from websites never runs as a formula."""
    table = [SHEET_COLUMNS]
    for r in rows:
        rights = RIGHTS_LABELS.get(r["rights"], r["rights"])
        table.append([safe_cell(search), hyperlink(r["link"], r["title"]), safe_cell(r["year"]), *(safe_cell(v) for v in (
            r["publisher"], r["country"], r["state"], r["website"], r["found_on"], r["on_amazon"], r["amazon_link"],
            r["amazon_best_rank"], r["pages"], r["cover"],
            f"{rights}: {r['rights_note']}" if r["rights_note"] else rights,
            r["topics"], r["found"],
        ))])  # fmt: skip
    return table


def as_table(rows: list[dict]) -> list[list[str]]:
    """Header plus rows, every cell made safe for spreadsheets."""
    return [COLUMNS] + [[safe_cell(r[c]) for c in COLUMNS] for r in rows]


def write_csv(rows: list[dict], path: Path, sheet_search: str | None = None) -> None:
    """The results table as CSV; with ``sheet_search``, in the Google Sheet layout."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(as_table(rows) if sheet_search is None else sheet_table(rows, sheet_search))


def _md(value: object) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def write_markdown(rows: list[dict], path: Path, heading: str, limit: int = 50) -> None:
    """A short table for reading on a phone (the GitHub Actions run summary)."""
    lines = [f"## {heading}", "", f"{len(rows)} book(s).", ""]
    states = any(r.get("state") for r in rows)  # a State column when state websites are listed
    if rows:
        lines += ["| Title | Year | Pages | Cover |" + (" State |" if states else "") + " Topics | Publisher | Rights |"
                  " On Amazon |", "|---|---|---|---|" + ("---|" if states else "") + "---|---|---|---|"]  # fmt: skip
        for r in rows[:limit]:
            title = _md(r["title"]).replace("[", "(").replace("]", ")")
            link = r["link"].replace(" ", "%20").replace(")", "%29")
            versions = int(r.get("versions") or 1)
            if versions > 1:
                title += f" ({versions} versions)"
            cells = [f"[{title}]({link})", str(r.get("year", "")), str(r["pages"]), r.get("cover", ""),
                     *([r["state"]] if states else []),
                     _md(r["topics"]),
                     _md(r["publisher"]), r["rights"], r["on_amazon"]]  # fmt: skip
            lines.append("| " + " | ".join(cells) + " |")
        if len(rows) > limit:
            lines += ["", f"... and {len(rows) - limit} more in the CSV files."]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def merge_csv(paths: list[Path]) -> tuple[list[str], list[list[str]]]:
    """The rows of CSV files written by export (one header, kept as written: safe cells and links)."""
    header: list[str] = []
    rows: list[list[str]] = []
    for path in paths:
        with path.open(newline="", encoding="utf-8") as fh:
            table = list(csv.reader(fh))
        if not table:
            continue
        if header and table[0] != header:
            raise ValueError(f"{path} has other columns than {paths[0]}")
        header = table[0]
        rows += table[1:]
    return header, rows
