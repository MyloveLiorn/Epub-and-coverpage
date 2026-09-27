from govbooks.agencies import AgencyIndex, render_tree, subtree_ids, tree_dict
from govbooks.agencies.federal_register import FEDERAL_ROOT_ID
from govbooks.agencies.wikidata import fetch_state_agencies
from tests.conftest import WIKIDATA_CALIFORNIA, binding


def test_federal_register_hierarchy(agencies):
    by_id = {a.id: a for a in agencies}
    assert by_id["fr:216"].parent_id == "fr:12"
    assert by_id["fr:12"].parent_id == FEDERAL_ROOT_ID
    assert by_id["fr:12"].website == "https://www.usda.gov/"
    assert "fr:999" not in by_id  # nameless entries are dropped


def test_wikidata_state_agencies(agencies):
    california = [a for a in agencies if a.jurisdiction == "California"]
    by_id = {a.id: a for a in california}
    assert len(california) == 4  # root + 3 agencies; duplicate and unlabeled rows dropped
    assert by_id["wd:Q1026960"].parent_id == "wd:Q5020012"
    assert by_id["wd:Q5020016"].parent_id == "us-state:CA"
    assert by_id["wd:Q5020016"].website == "https://water.ca.gov/"


def test_fetch_state_agencies_queries_each_state(fake_http):
    states = {"results": {"bindings": [
        binding(state="http://www.wikidata.org/entity/Q99", stateLabel="California"),
        binding(state="http://www.wikidata.org/entity/Q1439", stateLabel="Texas"),
    ]}}  # fmt: skip

    def answer(url, params, headers):
        if "Q35657" in params["query"]:
            return states
        assert "wd:Q99" in params["query"]
        return {"results": {"bindings": WIKIDATA_CALIFORNIA}}

    fake_http.add("GET", "query.wikidata.org", answer)
    result = fetch_state_agencies(fake_http, ["California"])
    assert {a.jurisdiction for a in result} == {"California"}
    assert len(fake_http.calls) == 2


def test_index_prefers_most_specific_agency(agencies):
    index = AgencyIndex(agencies)
    assert index.match(["United States. Department of Agriculture. Forest Service"]).agency_id == "fr:216"
    assert index.match(["Forest Service, U.S. Department of Agriculture"]).agency_id == "fr:216"
    assert index.match(["U.S. Dept. of Agriculture"]).agency_id == "fr:12"
    assert index.match(["USDA"]).agency_id == "fr:12"
    assert index.match([None, "United States. Environmental Protection Agency"]).agency_id == "fr:145"


def test_index_matches_state_agencies(agencies):
    index = AgencyIndex(agencies)
    match = index.match(["California. Department of Water Resources"])
    assert (match.agency_id, match.level, match.jurisdiction) == ("wd:Q5020016", "state", "California")
    # A label without the state name is only matched together with it.
    assert index.match(["California Department of Forestry and Fire Protection"]).agency_id == "wd:Q1026960"
    assert index.match(["Department of Forestry and Fire Protection"]) is None


def test_index_keeps_state_departments_away_from_federal_ones(agencies):
    match = AgencyIndex(agencies).match(["Minnesota Department of Agriculture"])
    assert (match.agency_id, match.level, match.jurisdiction) == (None, "state", "Minnesota")


def test_index_falls_back_to_publisher(agencies):
    index = AgencyIndex(agencies)
    match = index.match(["Smith, John", "U.S. Govt. Print. Off."])
    assert (match.agency_id, match.level) == (None, "federal")
    assert index.match(["Penguin Books"]) is None


def test_tree_rendering(agencies):
    lines = render_tree(agencies)
    usda = next(i for i, line in enumerate(lines) if "Agriculture Department" in line)
    assert lines[usda + 1].startswith("    - Forest Service")
    assert subtree_ids(agencies, "fr:12") == {"fr:12", "fr:216"}
    federal = next(node for node in tree_dict(agencies) if node["id"] == FEDERAL_ROOT_ID)
    assert {c["id"] for c in federal["children"]} == {"fr:12", "fr:145"}
