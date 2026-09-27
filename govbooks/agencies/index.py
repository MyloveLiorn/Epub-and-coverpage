"""Matching author/publisher strings from book records to agencies in the map."""

from __future__ import annotations

import re
from dataclasses import dataclass

from govbooks.models import Agency
from govbooks.states import US_STATES
from govbooks.text import contains_phrase, normalize_name

_STATE_NAMES = sorted((n.lower() for n in US_STATES.values()), key=len, reverse=True)

# Publisher strings that mark a US government publication even when no specific agency matches.
FEDERAL_PUBLISHER_MARKERS = [
    "government printing office",
    "government publishing office",
    "superintendent of documents",
    "gpo",
    "united states",
]
STATE_PUBLISHER_MARKERS = ["state printer", "state printing", "department", "division", "bureau",
                           "commission", "agency", "office", "board", "survey"]  # fmt: skip


@dataclass
class AgencyMatch:
    agency_id: str | None
    level: str
    jurisdiction: str
    name: str | None = None


def _leading_state(text: str) -> str | None:
    for name in _STATE_NAMES:
        if text == name or text.startswith(name + " "):
            return name
    return None


def _federal_aliases(agency: Agency) -> set[str]:
    name = normalize_name(agency.name)
    name = re.sub(r"^united states ", "", name)
    aliases = {name}
    # The Federal Register says "Agriculture Department"; catalogs say "Department of Agriculture".
    swapped = re.match(r"^(.+) department$", name)
    if swapped:
        aliases.add(f"department of {swapped.group(1)}")
    swapped = re.match(r"^department of (.+)$", name)
    if swapped:
        aliases.add(f"{swapped.group(1)} department")
    short = normalize_name(agency.short_name)
    if short and len(short.replace(" ", "")) >= 3:
        aliases.add(short)
    return {a for a in aliases if a}


def _state_aliases(agency: Agency) -> set[str]:
    state = agency.jurisdiction.lower()
    name = normalize_name(agency.name)
    aliases = set()
    # A bare "Department of Transportation" would match all 50 states, so always keep the state in it.
    aliases.add(name if contains_phrase(name, state) else f"{state} {name}")
    short = normalize_name(agency.short_name)
    if short and len(short.replace(" ", "")) >= 3:
        aliases.add(short if contains_phrase(short, state) else f"{state} {short}")
    return aliases


class AgencyIndex:
    """Finds the most specific agency named in a record's author or publisher strings."""

    def __init__(self, agencies: list[Agency]):
        self.by_id = {a.id: a for a in agencies}
        self._by_alias: dict[str, Agency] = {}
        for agency in agencies:
            if agency.source == "govbooks":  # synthetic roots, not real publishers
                continue
            aliases = _federal_aliases(agency) if agency.level == "federal" else _state_aliases(agency)
            for alias in aliases:
                self._by_alias.setdefault(alias, agency)
        self._longest = max((len(a.split()) for a in self._by_alias), default=0)

    def match(self, names: list[str | None]) -> AgencyMatch | None:
        found: list[tuple[int, Agency]] = []
        for raw in names:
            text = normalize_name(raw)
            if text:
                found.extend(self._aliases_in(text))
        if not found:
            return detect_government(names)
        # Catalogs write "United States. Department of Agriculture. Forest Service": both
        # agencies match, and the one we want is the most specific, so drop ancestors.
        ids = {agency.id for _, agency in found}
        specific = [(size, a) for size, a in found if not any(self._is_ancestor(a.id, other) for other in ids)]
        size, agency = max(specific or found, key=lambda pair: pair[0])
        return AgencyMatch(agency.id, agency.level, agency.jurisdiction, agency.name)

    def _aliases_in(self, text: str) -> list[tuple[int, Agency]]:
        """Aliases appearing as runs of whole words, skipping ones inside a longer match."""
        tokens = text.split()
        leading_state = _leading_state(text)
        spans: list[tuple[int, int]] = []
        found: list[tuple[int, Agency]] = []
        for size in range(min(self._longest, len(tokens)), 0, -1):
            for start in range(len(tokens) - size + 1):
                end = start + size
                if any(s <= start and end <= e for s, e in spans):
                    continue
                agency = self._by_alias.get(" ".join(tokens[start:end]))
                # "Minnesota Department of Agriculture" must not match the federal USDA.
                if agency and not (agency.level == "federal" and leading_state):
                    spans.append((start, end))
                    found.append((size, agency))
        return found

    def _is_ancestor(self, agency_id: str, other_id: str) -> bool:
        seen = set()
        current = self.by_id.get(other_id)
        while current and current.parent_id and current.parent_id not in seen:
            if current.parent_id == agency_id:
                return True
            seen.add(current.parent_id)
            current = self.by_id.get(current.parent_id)
        return False


def detect_government(names: list[str | None]) -> AgencyMatch | None:
    """Recognize a government publication by its publisher when no mapped agency matches."""
    for raw in names:
        text = normalize_name(raw)
        if not text:
            continue
        state = _leading_state(text)
        if state and any(contains_phrase(text, m) for m in STATE_PUBLISHER_MARKERS):
            return AgencyMatch(None, "state", state.title())
        if not state and any(contains_phrase(text, m) for m in FEDERAL_PUBLISHER_MARKERS):
            return AgencyMatch(None, "federal", "United States")
    return None
