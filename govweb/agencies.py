"""Adding agency websites to the map, from the Federal Register (federal) and Wikidata (states).

The .gov registry misses many agency sites: subdomains (water.ca.gov), and sites outside .gov
(army.mil, dot.state.tx.us). The agency directories list each agency's official website, so
importing them fills those gaps and names the office behind each subdomain.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import urlsplit

from govbooks.agencies import FEDERAL_ROOT_ID, fetch_federal_agencies, fetch_state_agencies
from govbooks.http import Http
from govbooks.models import Agency
from govbooks.states import STATE_ABBR
from govbooks.text import normalize_name
from govweb import db


def site_host(url: str | None) -> str | None:
    """ "https://www.fs.usda.gov/about" -> "fs.usda.gov" """
    if not url:
        return None
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    host = urlsplit(url).netloc.lower().split("@")[-1].split(":")[0].removeprefix("www.")
    return host if "." in host else None


_STATE_US_DOMAIN = re.compile(r"(^|\.)state\.[a-z]{2}\.us$")


def is_government_host(host: str) -> bool:
    """Agency directories carry stale or wrong websites (act.org for a defunct agency, archive
    copies on .edu sites). Only hosts on government domains are trusted as the agency's site."""
    return host.endswith((".gov", ".mil", ".fed.us")) or bool(_STATE_US_DOMAIN.search(host))


def _display_name(name: str, registry_names: dict[str, str]) -> str:
    """Use the registry's spelling where it names the same body: the Federal Register says
    "Agriculture Department", the registry "Department of Agriculture"."""
    normalized = normalize_name(name)
    candidates = [normalized]
    if normalized.endswith(" department"):
        candidates.append("department of " + normalized.removesuffix(" department"))
    for candidate in candidates:
        if candidate in registry_names:
            return registry_names[candidate]
    return name


def _top_agency(agency: Agency, by_id: dict[str, Agency]) -> Agency:
    """The department an agency belongs to (the level just under the federal root)."""
    current, seen = agency, set()
    while current.parent_id and current.parent_id in by_id and current.parent_id != FEDERAL_ROOT_ID:
        if current.id in seen:
            break
        seen.add(current.id)
        current = by_id[current.parent_id]
    return current


@dataclass
class ImportStats:
    agencies: int = 0
    with_website: int = 0
    new_sites: int = 0  # outside the registry: .mil, .us, ...
    new_subsites: int = 0  # subdomains of registry domains
    named_subsites: int = 0  # existing sub-sites given their agency's name
    not_government: int = 0  # websites outside government domains, not added
    removed_sites: int = 0  # earlier non-government agency sites taken off the map


def import_agencies(conn: sqlite3.Connection, agencies: list[Agency]) -> ImportStats:
    stats = ImportStats()
    by_id = {a.id: a for a in agencies}
    registry_names = {
        normalize_name(r["organization"]): r["organization"]
        for r in conn.execute("SELECT DISTINCT organization FROM sites WHERE source = 'registry'")
    }
    for agency in agencies:
        if agency.source == "govbooks":  # synthetic roots ("State of Ohio"), not agencies
            continue
        stats.agencies += 1
        host = site_host(agency.website)
        state = STATE_ABBR.get(agency.jurisdiction) if agency.level == "state" else None
        db.upsert_agency(
            conn,
            {"id": agency.id, "name": agency.name, "short_name": agency.short_name, "level": agency.level,
             "state": state, "parent_id": agency.parent_id, "website": agency.website, "host": host},
        )  # fmt: skip
        if not host:
            continue
        stats.with_website += 1
        if not is_government_host(host):
            stats.not_government += 1
            continue
        existing = db.get_site(conn, host)
        if existing is not None:
            if existing["source"] == "subdomain" and existing["suborganization"] in (None, ""):
                conn.execute("UPDATE sites SET suborganization = ? WHERE domain = ?", (agency.name, host))
                stats.named_subsites += 1
            continue
        parent = db.registered_domain_of(conn, host)
        if parent is not None:  # water.ca.gov under ca.gov
            stats.new_subsites += db.add_subsites(conn, parent, [host])
            conn.execute("UPDATE sites SET suborganization = ? WHERE domain = ?", (agency.name, host))
            continue
        if agency.level == "federal":
            top = _top_agency(agency, by_id)
            organization = _display_name(top.name, registry_names)
            suborganization = agency.name if top.id != agency.id else None
        else:
            organization, suborganization = agency.name, None
        stats.new_sites += db.add_site(
            conn, host, organization, level=agency.level, state=state, suborganization=suborganization,
            source="agency", domain_type="Agency website",
        )  # fmt: skip
    stats.removed_sites = db.remove_sites(
        conn,
        [r["domain"] for r in conn.execute("SELECT domain FROM sites WHERE source = 'agency'")
         if not is_government_host(r["domain"])],
    )  # fmt: skip
    conn.commit()
    return stats


def fetch_directory(http: Http, federal: bool = True, states: list[str] | None = None, progress=None) -> list[Agency]:
    """Download the agency directories. ``states`` None means all 50; [] means none."""
    agencies: list[Agency] = []
    if federal:
        if progress:
            progress("Federal agencies (Federal Register)...")
        agencies += fetch_federal_agencies(http)
    if states is None or states:
        if progress:
            progress("State agencies (Wikidata)...")
        agencies += fetch_state_agencies(http, states or None, progress=progress)
    return agencies
