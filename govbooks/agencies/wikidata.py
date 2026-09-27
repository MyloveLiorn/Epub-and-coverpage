"""State government agencies from Wikidata.

There is no single official directory of state agencies, so this asks Wikidata for
every current government agency whose jurisdiction (P1001) is a given state.
"""

from __future__ import annotations

from govbooks.http import Http
from govbooks.models import Agency
from govbooks.states import STATE_ABBR, US_STATES

SPARQL_URL = "https://query.wikidata.org/sparql"

STATES_QUERY = """
SELECT ?state ?stateLabel WHERE {
  ?state wdt:P31 wd:Q35657 .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

# P1001 = applies to jurisdiction, Q327333 = government agency, P576 = dissolved,
# P1813 = short name, P856 = official website, P749 = parent organization.
AGENCIES_QUERY = """
SELECT ?agency ?agencyLabel ?shortName ?website ?parent ?agencyDescription WHERE {
  ?agency wdt:P1001 wd:%s ;
          wdt:P31/wdt:P279* wd:Q327333 .
  FILTER NOT EXISTS { ?agency wdt:P576 ?dissolved }
  OPTIONAL { ?agency wdt:P1813 ?shortName . FILTER(LANG(?shortName) = "en") }
  OPTIONAL { ?agency wdt:P856 ?website }
  OPTIONAL { ?agency wdt:P749 ?parent }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""


def state_root_id(state: str) -> str:
    return f"us-state:{STATE_ABBR[state]}"


def state_root(state: str) -> Agency:
    return Agency(
        id=state_root_id(state),
        level="state",
        jurisdiction=state,
        name=f"State of {state}",
        short_name=STATE_ABBR[state],
        source="govbooks",
    )


def _query(http: Http, sparql: str) -> list[dict]:
    data = http.get_json(
        SPARQL_URL,
        params={"query": sparql, "format": "json"},
        headers={"Accept": "application/sparql-results+json"},
    )
    return data.get("results", {}).get("bindings", [])


def _value(row: dict, key: str) -> str | None:
    return row.get(key, {}).get("value") or None


def _qid(uri: str | None) -> str | None:
    return uri.rsplit("/", 1)[-1] if uri else None


def fetch_state_items(http: Http) -> dict[str, str]:
    """Map state name -> Wikidata QID."""
    states = {}
    for row in _query(http, STATES_QUERY):
        name = _value(row, "stateLabel")
        if name in STATE_ABBR:
            states[name] = _qid(_value(row, "state"))
    return states


def parse_state_agencies(state: str, rows: list[dict]) -> list[Agency]:
    agencies: dict[str, Agency] = {}
    parents: dict[str, str | None] = {}
    for row in rows:
        qid = _qid(_value(row, "agency"))
        label = _value(row, "agencyLabel")
        # Items without an English label come back labelled with their QID.
        if not qid or not label or label == qid or qid in agencies:
            continue
        agencies[qid] = Agency(
            id=f"wd:{qid}",
            level="state",
            jurisdiction=state,
            name=label,
            short_name=_value(row, "shortName"),
            website=_value(row, "website"),
            description=_value(row, "agencyDescription"),
            source="wikidata.org",
        )
        parents[qid] = _qid(_value(row, "parent"))
    root = state_root(state)
    for qid, agency in agencies.items():
        parent = parents[qid]
        # Parents outside this state's agency set (e.g. "Government of X") hang off the state root.
        agency.parent_id = f"wd:{parent}" if parent in agencies and parent != qid else root.id
    return [root, *agencies.values()]


def fetch_state_agencies(http: Http, states: list[str] | None = None, progress=None) -> list[Agency]:
    wanted = states or list(US_STATES.values())
    items = fetch_state_items(http)
    result: list[Agency] = []
    for state in wanted:
        qid = items.get(state)
        if not qid:
            if progress:
                progress(f"  {state}: not found on Wikidata, skipped")
            continue
        agencies = parse_state_agencies(state, _query(http, AGENCIES_QUERY % qid))
        if progress:
            progress(f"  {state}: {len(agencies) - 1} agencies")
        result.extend(agencies)
    return result
