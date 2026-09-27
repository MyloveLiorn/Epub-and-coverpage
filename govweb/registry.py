"""The official list of every .gov domain, published by CISA (github.com/cisagov/dotgov-data)."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

REGISTRY_URL = "https://raw.githubusercontent.com/cisagov/dotgov-data/main/current-full.csv"

# "Domain type" values and the level of government they belong to.
LEVELS = {
    "federal": "federal",
    "federal - executive": "federal",
    "federal - legislative": "federal",
    "federal - judicial": "federal",
    "state or territory": "state",
    "state": "state",
    "interstate": "interstate",
    "county": "county",
    "city": "city",
    "special district": "special_district",
    "independent intrastate": "special_district",
    "school district": "school_district",
    "tribal": "tribal",
    "native sovereign nation": "tribal",
}
LEVEL_NAMES = sorted(set(LEVELS.values()))


@dataclass
class Site:
    domain: str
    domain_type: str
    level: str
    election: bool
    organization: str
    suborganization: str | None = None
    city: str | None = None
    state: str | None = None


def _column(row: dict[str, str], *names: str) -> str:
    """Read a column by any of its historical header spellings, ignoring case."""
    lowered = {k.strip().lower(): v for k, v in row.items() if k}
    for name in names:
        value = lowered.get(name)
        if value is not None:
            return value.strip()
    return ""


def level_of(domain_type: str) -> tuple[str, bool]:
    """("City - Election") -> ("city", True). Unknown types keep their own lowercase name."""
    kind = domain_type.strip().lower()
    election = kind.endswith(" - election")
    if election:
        kind = kind[: -len(" - election")]
    return LEVELS.get(kind, kind.replace(" ", "_") or "unknown"), election


def parse_registry(text: str) -> list[Site]:
    sites = []
    for row in csv.DictReader(io.StringIO(text)):
        domain = _column(row, "domain name").lower()
        if not domain:
            continue
        domain_type = _column(row, "domain type")
        level, election = level_of(domain_type)
        if _column(row, "agency"):  # older files: Agency = parent, Organization = sub-unit
            organization, suborganization = _column(row, "agency"), _column(row, "organization")
        else:
            organization, suborganization = _column(row, "organization name"), _column(row, "suborganization name")
        # The security contact column is deliberately not kept: the map doesn't need it.
        sites.append(
            Site(
                domain=domain,
                domain_type=domain_type,
                level=level,
                election=election,
                organization=organization,
                suborganization=suborganization or None,
                city=_column(row, "city") or None,
                state=_column(row, "state") or None,
            )
        )
    return sites
