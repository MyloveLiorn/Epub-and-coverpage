"""Federal agencies and their parent/child structure from the Federal Register API (no key needed)."""

from __future__ import annotations

from govbooks.http import Http
from govbooks.models import Agency

AGENCIES_URL = "https://www.federalregister.gov/api/v1/agencies.json"
FEDERAL_ROOT_ID = "us:federal"


def federal_root() -> Agency:
    return Agency(
        id=FEDERAL_ROOT_ID,
        level="federal",
        jurisdiction="United States",
        name="Federal Government of the United States",
        short_name="US",
        website="https://www.usa.gov/agency-index",
        source="govbooks",
    )


def parse_agencies(data: list[dict]) -> list[Agency]:
    agencies = [federal_root()]
    for item in data:
        if not item.get("name"):
            continue
        parent = item.get("parent_id")
        agencies.append(
            Agency(
                id=f"fr:{item['id']}",
                level="federal",
                jurisdiction="United States",
                name=item["name"].strip(),
                short_name=(item.get("short_name") or "").strip() or None,
                parent_id=f"fr:{parent}" if parent else FEDERAL_ROOT_ID,
                website=item.get("agency_url") or item.get("url"),
                description=item.get("description"),
                source="federalregister.gov",
            )
        )
    return agencies


def fetch_federal_agencies(http: Http) -> list[Agency]:
    return parse_agencies(http.get_json(AGENCIES_URL))
