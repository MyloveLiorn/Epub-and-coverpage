"""The .gov map as a tree: level of government, then state or organization, down to domains."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

# Levels whose sites are organized by organization; the rest are organized by state first.
NATIONAL_LEVELS = {"federal", "interstate", "tribal", "other"}


@dataclass
class Node:
    label: str
    domains: list[sqlite3.Row] = field(default_factory=list)
    children: dict[str, "Node"] = field(default_factory=dict)

    def child(self, label: str) -> "Node":
        return self.children.setdefault(label, Node(label))

    @property
    def size(self) -> int:
        return len(self.domains) + sum(c.size for c in self.children.values())

    @property
    def documents(self) -> int:
        return sum(d["documents_found"] or 0 for d in self.domains) + sum(c.documents for c in self.children.values())


def build_tree(sites: list[sqlite3.Row]) -> Node:
    root = Node("United States .gov")
    for site in sites:
        node = root.child(site["level"])
        if site["level"] in NATIONAL_LEVELS:
            node = node.child(site["organization"] or "(unnamed)")
            if site["suborganization"]:
                node = node.child(site["suborganization"])
        else:
            node = node.child(site["state"] or "(no state)").child(site["organization"] or "(unnamed)")
        node.domains.append(site)
    return root


def _domain_line(site: sqlite3.Row) -> str:
    name = site["domain"] + (f"  [sub-site of {site['parent_domain']}]" if site["parent_domain"] else "")
    if site["crawled_at"] is None:
        return name
    if site["crawl_status"] != "ok":
        return f"{name}  ({site['crawl_status']})"
    return f"{name}  ({site['documents_found'] or 0} documents)"


def render(root: Node, max_depth: int | None = None) -> list[str]:
    lines: list[str] = []

    def walk(node: Node, depth: int) -> None:
        docs = f", {node.documents} documents" if node.documents else ""
        lines.append(f"{'  ' * depth}{node.label} ({node.size} domains{docs})")
        if max_depth is not None and depth >= max_depth:
            return
        for child in sorted(node.children.values(), key=lambda c: (-c.size, c.label)):
            walk(child, depth + 1)
        for site in node.domains:
            lines.append(f"{'  ' * (depth + 1)}- {_domain_line(site)}")

    for child in sorted(root.children.values(), key=lambda c: (-c.size, c.label)):
        walk(child, 0)
    return lines


def to_dict(node: Node) -> dict:
    return {
        "name": node.label,
        "domains": node.size,
        "documents": node.documents,
        "sites": [
            {"domain": s["domain"], "parent_domain": s["parent_domain"], "city": s["city"], "home_url": s["home_url"],
             "crawl_status": s["crawl_status"], "documents_found": s["documents_found"]}
            for s in node.domains
        ],  # fmt: skip
        "children": [to_dict(c) for c in sorted(node.children.values(), key=lambda c: (-c.size, c.label))],
    }
