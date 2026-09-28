"""Each state's official government website ("portal": texas.gov, ca.gov, myflorida.com), where a
state's search starts: portals link to the state's agencies and their publications."""

from __future__ import annotations

import json
import sqlite3
from functools import cache
from importlib import resources

from govbooks.states import US_STATES
from govweb import db

DOMAIN_TYPE = "State portal"


@cache
def state_portals() -> dict[str, str]:
    """{"TX": "texas.gov", ...} for the 50 states."""
    data = json.loads(resources.files("govweb").joinpath("data/state_portals.json").read_text())
    return {abbr: host for abbr, host in data.items() if not abbr.startswith("_")}


def is_portal(host: str) -> bool:
    return host in state_portals().values()


def ensure_portal(conn: sqlite3.Connection, abbr: str) -> str:
    """Put a state's portal on the map as a state-level site of that state; returns its host."""
    abbr = abbr.upper()
    host = state_portals()[abbr]
    name = f"State of {US_STATES[abbr]}"
    site = db.get_site(conn, host)
    if site is None:
        parent = db.registered_domain_of(conn, host)
        if parent is not None:
            db.add_subsites(conn, parent, [host])
        else:  # outside the .gov registry, like myflorida.com
            db.add_site(conn, host, organization=name, level="state", state=abbr, source="agency",
                        domain_type=DOMAIN_TYPE)  # fmt: skip
    elif site["level"] == "other":  # added by hand earlier, without an owner
        conn.execute("UPDATE sites SET organization = ?, level = 'state', state = ? WHERE domain = ?", (name, abbr, host))
        conn.commit()
    return host
