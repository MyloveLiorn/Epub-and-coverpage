"""Copyright screening for government works: federal rules and exceptions, and each state's policy.

This is a first screening to decide what to look at more closely, not legal advice. The state
table (data/state_copyright.json) records what each state's statutes, attorney general opinions
and court cases say about copyright in the state's own works, with sources.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from importlib import resources

from govbooks.states import STATE_ABBR, US_STATES
from govbooks.text import contains_phrase, normalize_name

# Screening results, from most to least reusable.
PUBLIC_DOMAIN = "public_domain"  # out of copyright, or the source says so
LIKELY_PUBLIC_DOMAIN = "likely_public_domain"  # the rule for this government says it is free
CHECK = "check"  # depends on the item; read its rights notice
LIKELY_COPYRIGHTED = "likely_copyrighted"  # the government claims copyright; ask for permission
UNKNOWN = "unknown"  # not recognizably a government work
RIGHTS_VALUES = [PUBLIC_DOMAIN, LIKELY_PUBLIC_DOMAIN, CHECK, LIKELY_COPYRIGHTED, UNKNOWN]
REUSABLE = [PUBLIC_DOMAIN, LIKELY_PUBLIC_DOMAIN]

FEDERAL_RULE = (
    "US federal government work: not protected by copyright in the US (17 U.S.C. 105). Contractor- or "
    "grantee-written parts and third-party photos or text inside it can still be copyrighted, and the US "
    "may claim copyright in other countries, which matters when choosing sales territories."
)
LOCAL_RULE = "Local government work: copyright follows state law and local practice. Check the item."
TRIBAL_RULE = "Tribal nations are sovereign governments; US federal and state rules do not apply. Ask first."
INTERSTATE_RULE = "Interstate body: governed by its compact and member states. Check the item."
EXTENSION_RULE = (
    "Cooperative Extension and other university publications are usually issued by the university, "
    "which often holds the copyright. Check the item's notice."
)


@dataclass(frozen=True)
class StatePolicy:
    abbr: str
    state: str
    status: str  # public_domain, claims_copyright, mixed, unclear
    summary: str
    key_law: str | None = None
    sources: tuple[str, ...] = ()
    confidence: str = "low"


@dataclass(frozen=True)
class FederalException:
    name: str
    names: tuple[str, ...]  # normalized organization names that identify it
    domains: tuple[str, ...]
    summary: str
    sources: tuple[str, ...] = field(default=())


FEDERAL_EXCEPTIONS = [
    FederalException(
        "US Postal Service",
        ("postal service", "united states postal service", "usps"),
        ("usps.com", "usps.gov", "uspis.gov", "postalpro.usps.com"),
        "17 U.S.C. 105 does not cover Postal Service works; USPS holds copyright in stamp designs issued "
        "after 1978 and can hold it in its other works.",
        ("https://www.law.cornell.edu/uscode/text/17/105",),
    ),
    FederalException(
        "Smithsonian Institution",
        ("smithsonian", "smithsonian institution"),
        ("si.edu",),
        "Only works by the Smithsonian's federally paid employees are public domain; works by its trust-fund "
        "employees and contractors (much of its publishing) can be copyrighted. Check each item.",
        ("https://www.si.edu/ogc/legalhistory", "https://www.si.edu/termsofuse"),
    ),
    FederalException(
        "Federal Reserve Banks",
        ("federal reserve bank",),
        ("stlouisfed.org", "newyorkfed.org", "bostonfed.org", "philadelphiafed.org", "clevelandfed.org",
         "richmondfed.org", "atlantafed.org", "chicagofed.org", "minneapolisfed.org", "kansascityfed.org",
         "dallasfed.org", "frbsf.org", "frbservices.org"),  # fmt: skip
        "The regional Federal Reserve Banks are not part of the federal government and hold copyright in "
        "their publications. The Board of Governors' own works are federal.",
        ("https://www.stlouisfed.org/about-us/legal-information",),
    ),
    FederalException(
        "NIST Standard Reference Data",
        ("standard reference data",),
        (),
        "NIST Standard Reference Data can be copyrighted under the Standard Reference Data Act "
        "(15 U.S.C. 290e); other NIST works by employees are public domain.",
        ("https://www.nist.gov/open/license",),
    ),
]


@lru_cache(maxsize=1)
def state_policies() -> dict[str, StatePolicy]:
    """Policies by postal abbreviation."""
    try:
        text = resources.files("govbooks").joinpath("data/state_copyright.json").read_text()
    except FileNotFoundError:
        return {}
    policies = {}
    for item in json.loads(text):
        policies[item["abbr"].upper()] = StatePolicy(
            abbr=item["abbr"].upper(),
            state=item["state"],
            status=item["status"],
            summary=item["summary"],
            key_law=item.get("key_law"),
            sources=tuple(item.get("sources") or ()),
            confidence=item.get("confidence", "low"),
        )
    return policies


def state_policy(state: str | None) -> StatePolicy | None:
    if not state:
        return None
    key = state.strip()
    abbr = key.upper() if key.upper() in US_STATES else STATE_ABBR.get(key.title())
    return state_policies().get(abbr) if abbr else None


def federal_exception(names: list[str | None] | tuple = (), domain: str | None = None) -> FederalException | None:
    host = (domain or "").lower().removeprefix("www.")
    normalized = [normalize_name(n) for n in names if n]
    for exc in FEDERAL_EXCEPTIONS:
        if host and any(host == d or host.endswith("." + d) for d in exc.domains):
            return exc
        if any(contains_phrase(text, name) for text in normalized for name in exc.names):
            return exc
    return None


def _looks_like_extension(names: list[str | None] | tuple, domain: str | None) -> bool:
    if domain and (domain.endswith(".edu") or "extension" in domain):
        return True
    return any(contains_phrase(normalize_name(n), w) for n in names if n for w in ("extension", "university"))


@dataclass(frozen=True)
class Rights:
    status: str
    note: str


def assess(
    level: str | None,
    state: str | None = None,
    names: list[str | None] | tuple = (),
    domain: str | None = None,
    year: int | None = None,
    source_says_pd: bool | None = None,
    today: date | None = None,
) -> Rights:
    """Screen one work. ``state`` is a name or postal code; ``names`` are its authors, publisher
    and owning organization; ``domain`` is the website it came from, if any."""
    today = today or date.today()
    # US works published 95+ years ago are out of copyright on 1 January of the 96th year.
    if year and year <= today.year - 96:
        return Rights(PUBLIC_DOMAIN, f"Published {year}; its US copyright term has ended.")
    if source_says_pd:
        return Rights(PUBLIC_DOMAIN, "The source lists it as public domain or a free public ebook.")
    if level == "federal":
        exception = federal_exception(names, domain)
        if exception:
            return Rights(CHECK, f"{exception.name}: {exception.summary}")
        return Rights(LIKELY_PUBLIC_DOMAIN, FEDERAL_RULE)
    if level == "state":
        if _looks_like_extension(names, domain):
            return Rights(CHECK, EXTENSION_RULE)
        policy = state_policy(state)
        if policy is None:
            return Rights(CHECK, f"No copyright policy on file for {state or 'this state'}. Check the item.")
        prefix = f"{policy.state} ({policy.status.replace('_', ' ')}, {policy.confidence} confidence): "
        if policy.status == "public_domain":
            # Only well-established rules count as reusable without a closer look.
            status = LIKELY_PUBLIC_DOMAIN if policy.confidence == "high" else CHECK
        else:
            status = LIKELY_COPYRIGHTED if policy.status == "claims_copyright" else CHECK
        return Rights(status, prefix + policy.summary)
    if level in ("county", "city", "special_district", "school_district"):
        policy = state_policy(state)
        extra = f" {policy.state}'s state-level rule: {policy.status.replace('_', ' ')}." if policy else ""
        return Rights(CHECK, LOCAL_RULE + extra)
    if level == "tribal":
        return Rights(CHECK, TRIBAL_RULE)
    if level == "interstate":
        return Rights(CHECK, INTERSTATE_RULE)
    return Rights(UNKNOWN, "Government authorship not confirmed. Verify copyright before reuse.")
