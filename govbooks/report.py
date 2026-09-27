"""Exporting the book list as CSV or Markdown."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from govbooks import db
from govbooks.market import VERDICT_HELP, search_query

COLUMNS = [
    "id", "title", "year", "agency", "level", "rights", "topics", "topic_score", "verdict", "market_score",
    "best_rank", "median_price", "matching_listings", "provider", "fulltext_url", "amazon_search", "sources",
]  # fmt: skip


def amazon_search_url(title: str, marketplace: str = "www.amazon.com") -> str:
    return f"https://{marketplace}/s?" + urlencode({"k": search_query(title), "i": "stripbooks"})


def book_row(book: sqlite3.Row, marketplace: str = "www.amazon.com") -> dict:
    return {
        "id": book["id"],
        "title": book["title"],
        "year": book["year"],
        "agency": book["agency_name"] or (book["level"] or "").title(),
        "level": book["level"],
        "rights": book["rights"],
        "topics": book["topics"],
        "topic_score": book["topic_score"],
        "verdict": book["verdict"],
        "market_score": book["market_score"],
        "best_rank": book["best_rank"],
        "median_price": book["median_price"],
        "matching_listings": book["matching_listings"],
        "provider": book["provider"],
        "fulltext_url": book["fulltext_url"],
        "amazon_search": amazon_search_url(book["title"], marketplace),
        "sources": " ".join(s["url"] for s in db.loads(book["sources"]) if s.get("url")),
    }


def write_csv(books: list[sqlite3.Row], path: Path, marketplace: str = "www.amazon.com") -> None:
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for book in books:
            writer.writerow(book_row(book, marketplace))


def _cell(value: object) -> str:
    return "" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def write_markdown(books: list[sqlite3.Row], path: Path, title: str, marketplace: str = "www.amazon.com") -> None:
    lines = [f"# {title}", "", f"{len(books)} book(s), best market score first. Generated {db.now()}.", ""]
    lines += ["| Score | Verdict | Title | Year | Agency | Rights | Full text | Amazon |", "|---|---|---|---|---|---|---|---|"]
    for book in books:
        row = book_row(book, marketplace)
        fulltext = f"[open]({row['fulltext_url']})" if row["fulltext_url"] else ""
        lines.append(
            "| "
            + " | ".join(
                _cell(v)
                for v in (
                    row["market_score"] if row["market_score"] is not None else "-",
                    row["verdict"] or "not checked",
                    row["title"],
                    row["year"],
                    row["agency"],
                    row["rights"],
                    fulltext,
                    f"[search]({row['amazon_search']})",
                )
            )
            + " |"
        )
    lines += ["", "## Verdicts", ""]
    lines += [f"- **{name}**: {text}" for name, text in VERDICT_HELP.items()]
    lines += [
        "",
        "Rights are a first screening, not legal advice. Federal works are generally public domain in the US,",
        "but can include copyrighted third-party material; state works need checking state by state.",
        "",
    ]
    path.write_text("\n".join(lines))
