"""Sites and paths that are never searched or listed (govweb/data/excluded.txt)."""

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
