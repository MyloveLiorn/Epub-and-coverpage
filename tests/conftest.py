from __future__ import annotations

from typing import Any, Callable

import pytest

from govbooks import db
from govbooks.agencies.federal_register import parse_agencies
from govbooks.agencies.wikidata import parse_state_agencies


class FakeHttp:
    """Answers requests from a list of (method, url fragment, response) routes."""

    def __init__(self, routes: list[tuple[str, str, Any]] | None = None):
        self.routes = routes or []
        self.calls: list[tuple[str, str, dict]] = []

    def add(self, method: str, fragment: str, response: Any | Callable[..., Any]) -> None:
        self.routes.append((method, fragment, response))

    def _answer(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append((method, url, kwargs))
        for route_method, fragment, response in self.routes:
            if route_method == method and fragment in url:
                return response(url, **kwargs) if callable(response) else response
        raise AssertionError(f"unexpected request: {method} {url} {kwargs.get('params')}")

    def get_json(self, url, params=None, headers=None):
        return self._answer("GET", url, params=params, headers=headers)

    def post_json(self, url, json=None, data=None, params=None, headers=None):
        return self._answer("POST", url, json=json, data=data, params=params, headers=headers)


FEDERAL_REGISTER = [
    {"id": 12, "name": "Agriculture Department", "short_name": "USDA", "parent_id": None,
     "agency_url": "https://www.usda.gov/", "description": "The Department of Agriculture."},
    {"id": 216, "name": "Forest Service", "short_name": "FS", "parent_id": 12,
     "agency_url": "https://www.fs.usda.gov/", "description": None},
    {"id": 145, "name": "Environmental Protection Agency", "short_name": "EPA", "parent_id": None,
     "agency_url": "https://www.epa.gov/", "description": None},
    {"id": 999, "name": "", "short_name": None, "parent_id": None},
]  # fmt: skip


def binding(**values: str) -> dict:
    return {k: {"type": "uri" if v.startswith("http") else "literal", "value": v} for k, v in values.items()}


WIKIDATA_CALIFORNIA = [
    binding(agency="http://www.wikidata.org/entity/Q5020016", agencyLabel="California Department of Water Resources",
            shortName="DWR", website="https://water.ca.gov/"),
    binding(agency="http://www.wikidata.org/entity/Q5020012", agencyLabel="California Natural Resources Agency"),
    binding(agency="http://www.wikidata.org/entity/Q1026960", agencyLabel="Department of Forestry and Fire Protection",
            shortName="CAL FIRE", parent="http://www.wikidata.org/entity/Q5020012"),
    # Duplicate row for a second website: kept once.
    binding(agency="http://www.wikidata.org/entity/Q5020016", agencyLabel="California Department of Water Resources",
            website="https://water.ca.gov/other"),
    # No English label: skipped.
    binding(agency="http://www.wikidata.org/entity/Q999999", agencyLabel="Q999999"),
]  # fmt: skip


@pytest.fixture
def agencies():
    return parse_agencies(FEDERAL_REGISTER) + parse_state_agencies("California", WIKIDATA_CALIFORNIA)


@pytest.fixture
def conn(agencies):
    connection = db.connect(":memory:")
    db.upsert_agencies(connection, agencies)
    yield connection
    connection.close()


@pytest.fixture
def fake_http():
    return FakeHttp()
