"""The map of federal and state government offices."""

from __future__ import annotations

from collections import defaultdict

from govbooks.agencies.federal_register import FEDERAL_ROOT_ID, fetch_federal_agencies
from govbooks.agencies.index import AgencyIndex, AgencyMatch
from govbooks.agencies.wikidata import fetch_state_agencies, state_root_id
from govbooks.models import Agency

__all__ = [
    "AgencyIndex",
    "AgencyMatch",
    "FEDERAL_ROOT_ID",
    "build_tree",
    "fetch_federal_agencies",
    "fetch_state_agencies",
    "render_tree",
    "state_root_id",
    "subtree_ids",
    "tree_dict",
]


def build_tree(agencies: list[Agency]) -> tuple[list[Agency], dict[str, list[Agency]]]:
    """Roots and a parent id -> children mapping. Agencies whose parent is missing become roots."""
    by_id = {a.id: a for a in agencies}
    children: dict[str, list[Agency]] = defaultdict(list)
    roots = []
    for agency in agencies:
        if agency.parent_id and agency.parent_id in by_id and agency.parent_id != agency.id:
            children[agency.parent_id].append(agency)
        else:
            roots.append(agency)
    for kids in children.values():
        kids.sort(key=lambda a: a.name.lower())
    roots.sort(key=lambda a: (a.level != "federal", a.name.lower()))
    return roots, children


def render_tree(agencies: list[Agency], root_id: str | None = None, max_depth: int | None = None) -> list[str]:
    roots, children = build_tree(agencies)
    if root_id:
        roots = [a for a in agencies if a.id == root_id]
    lines: list[str] = []
    seen: set[str] = set()

    def walk(agency: Agency, depth: int) -> None:
        if agency.id in seen:  # guard against cycles in crowd-sourced data
            return
        seen.add(agency.id)
        short = f" ({agency.short_name})" if agency.short_name and agency.short_name != agency.name else ""
        lines.append(f"{'  ' * depth}- {agency.name}{short}  [{agency.id}]")
        if max_depth is not None and depth >= max_depth:
            return
        for child in children.get(agency.id, []):
            walk(child, depth + 1)

    for root in roots:
        walk(root, 0)
    return lines


def tree_dict(agencies: list[Agency]) -> list[dict]:
    """Nested JSON-friendly form of the whole map."""
    roots, children = build_tree(agencies)
    seen: set[str] = set()

    def node(agency: Agency) -> dict:
        seen.add(agency.id)
        return {
            "id": agency.id,
            "name": agency.name,
            "short_name": agency.short_name,
            "level": agency.level,
            "jurisdiction": agency.jurisdiction,
            "website": agency.website,
            "children": [node(c) for c in children.get(agency.id, []) if c.id not in seen],
        }

    return [node(r) for r in roots]


def subtree_ids(agencies: list[Agency], root_id: str) -> set[str]:
    _, children = build_tree(agencies)
    result, stack = set(), [root_id]
    while stack:
        current = stack.pop()
        if current in result:
            continue
        result.add(current)
        stack.extend(c.id for c in children.get(current, []))
    return result
