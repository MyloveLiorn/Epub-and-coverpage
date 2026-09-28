"""Sites and paths that are never searched or listed (govweb/data/excluded.txt), and states whose
own publications can't be reused (govweb/data/excluded_states.txt)."""

from __future__ import annotations

from functools import cache
from importlib import resources
from urllib.parse import urlsplit


@cache
def excluded() -> tuple[str, ...]:
    text = resources.files("govweb").joinpath("data/excluded.txt").read_text()
    lines = (line.split("#", 1)[0].strip().lower() for line in text.splitlines())
    return tuple(line.removeprefix("https://").removeprefix("http://").removeprefix("www.") for line in lines if line)


def is_excluded(url: str, patterns: tuple[str, ...] | None = None) -> bool:
    """Whether a URL (or a bare host) falls under an excluded site or path."""
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url.lower())
    host = parts.netloc.split(":")[0].removeprefix("www.")
    for pattern in excluded() if patterns is None else patterns:
        site, _, path = pattern.partition("/")
        if host != site and not host.endswith("." + site):
            continue
        if not path or parts.path.lstrip("/").startswith(path):
            return True
    return False


@cache
def excluded_states() -> frozenset[str]:
    """Two-letter codes of the states whose own sites are never searched or listed."""
    text = resources.files("govweb").joinpath("data/excluded_states.txt").read_text()
    words = (w for line in text.splitlines() for w in line.split("#", 1)[0].split())
    return frozenset(w.upper() for w in words if len(w) == 2 and w.isalpha())


def is_excluded_state_site(level: str | None, state: str | None) -> bool:
    """Whether a site belongs to the government of an excluded state (not its cities or counties)."""
    return level == "state" and (state or "").upper() in excluded_states()
