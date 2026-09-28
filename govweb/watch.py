"""Watches: saved searches that report new books as they appear on government websites."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from govbooks import copyright_policy
from govbooks.text import contains_phrase, normalize_text
from govweb.classify import title_from_url


def keyword_list(value: str | None) -> list[str]:
    return [k.strip() for k in (value or "").split(",") if k.strip()]


Matcher = Callable[[sqlite3.Row], int]


def _topic_matcher(spec) -> Matcher:
    from govbooks.discover import score_topic
    from govbooks.models import Record

    def by_topic(doc: sqlite3.Row) -> int:
        record = Record("web", doc["url"], doc["title"], description=title_from_url(doc["url"]))
        score = score_topic(spec, record)
        return score if score >= spec.min_score else 0

    return by_topic


def topic_matchers(config_path: Path | None = None, names: list[str] | None = None) -> dict[str, Matcher]:
    """A scorer per topic in govbooks.toml (or just the named ones)."""
    from govbooks.config import load_config

    config = load_config(config_path)
    return {name: _topic_matcher(config.topic(name)) for name in (names or list(config.topics))}


def topic_keywords(config_path: Path | None = None, names: list[str] | None = None) -> list[str]:
    """The keywords of the named topics (every topic when names is empty)."""
    from govbooks.config import load_config

    config = load_config(config_path)
    return [k for name in (names or list(config.topics)) for k in config.topic(name).keywords]


def matching_topics(doc: sqlite3.Row, matchers: dict[str, Matcher]) -> list[str]:
    return [name for name, matcher in matchers.items() if matcher(doc)]


def make_matcher(topic: str | None, keywords: list[str], config_path: Path | None = None) -> Matcher:
    """A scorer for documents: 0 means no match. Topics come from govbooks.toml; plain
    keywords count one point per keyword in the title or file name."""
    if topic:
        return topic_matchers(config_path, [topic])[topic]
    if keywords:
        phrases = [normalize_text(k) for k in keywords]

        def by_keywords(doc: sqlite3.Row) -> int:
            text = normalize_text(f"{doc['title']} {title_from_url(doc['url'])}")
            return sum(contains_phrase(text, p) for p in phrases)

        return by_keywords
    return lambda doc: 1


@dataclass
class Found:
    doc: sqlite3.Row
    score: int
    rights: copyright_policy.Rights


def rights_of(doc: sqlite3.Row) -> copyright_policy.Rights:
    """Screen a document by who published the site it came from."""
    return copyright_policy.assess(
        doc["level"],
        state=doc["state"],
        names=[doc["organization"], doc["suborganization"]],
        domain=doc["domain"],
    )


def rank(docs: list[sqlite3.Row], matcher: Callable[[sqlite3.Row], int], reusable_only: bool = False) -> list[Found]:
    found = []
    for doc in docs:
        score = matcher(doc)
        if not score:
            continue
        rights = rights_of(doc)
        if reusable_only and rights.status not in copyright_policy.REUSABLE:
            continue
        found.append(Found(doc, score, rights))
    return sorted(found, key=lambda f: (-f.score, -f.doc["book_score"], f.doc["title"]))


def markdown_report(sections: list[tuple[str, str, list[Found]]], stamp: str) -> str:
    """sections: (watch name, description, found)."""
    lines = [f"# New government books ({stamp[:10]})", ""]
    for name, description, found in sections:
        lines += [f"## {name}", "", f"{description}. {len(found)} new.", ""]
        if not found:
            continue
        lines += ["| Title | Type | Publisher | Rights | Link |", "|---|---|---|---|---|"]
        for item in found:
            doc = item.doc
            publisher = doc["suborganization"] or doc["organization"]
            cells = [doc["title"], doc["file_type"], f"{publisher} ({doc['domain']})", item.rights.status,
                     f"[open]({doc['url']})"]  # fmt: skip
            lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |")
        lines.append("")
    lines += ["Rights are a first screening, not legal advice. Read each item's own notice before reuse.", ""]
    return "\n".join(lines)
