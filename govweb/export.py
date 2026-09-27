"""The results table: book-like documents with their publisher, copyright screening and Amazon status."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from govweb.amazon import amazon_link, amazon_status
from govweb.watch import rights_of

COLUMNS = [
    "found", "new", "title", "type", "publisher", "level", "state", "website", "link", "rights",
    "on_amazon", "amazon_editions", "amazon_best_rank", "amazon_link", "book_score", "found_on", "rights_note",
]  # fmt: skip


def safe_cell(value: object) -> str:
    """Text from websites must not become a spreadsheet formula when the file is opened."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "@", "\t", "\r") or (text[:1] == "-" and not text[1:2].isdigit()):
        return "'" + text
    return text


def result_rows(rows: list[sqlite3.Row], marketplace: str = "www.amazon.com") -> list[dict]:
    out = []
    for row in rows:
        rights = rights_of(row)
        out.append(
            {
                "found": row["first_seen"][:10],
                "new": "yes" if row["first_crawled_at"] and row["first_seen"] > row["first_crawled_at"] else "",
                "title": row["title"],
                "type": row["file_type"],
                "publisher": row["suborganization"] or row["organization"],
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


def as_table(rows: list[dict]) -> list[list[str]]:
    """Header plus rows, every cell made safe for spreadsheets."""
    return [COLUMNS] + [[safe_cell(r[c]) for c in COLUMNS] for r in rows]


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(as_table(rows))
