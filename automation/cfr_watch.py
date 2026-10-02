"""Which Code of Federal Regulations volumes has GovInfo published for a year?

Runs daily in GitHub Actions (.github/workflows/cfr-watch.yml) so the book PC
does not have to ask GovInfo itself. It lists every CFR package of the edition
year (titles 1-50), compares with the previous run, and writes two files:

    cfr-<year>-published.json  every volume GovInfo has, with the date we first saw it
    cfr-<year>-new.json        the volumes first seen in THIS run (empty list on a quiet day)

Public data only: package ids and dates. No key is stored here - set the
GOVINFO_API_KEY secret (free at api.data.gov); DEMO_KEY is used when it is unset.
Standard library only.

    python automation/cfr_watch.py --year 2026 --out data
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PKG_RE = re.compile(r"^CFR-(\d{4})-title(\d+)-vol(\d+)$")
KEY = os.environ.get("GOVINFO_API_KEY") or "DEMO_KEY"


def get(url: str) -> dict:
    for attempt in range(6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cfr-watch (github actions)"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except Exception as exc:  # noqa: BLE001 - network: retry, then give up loudly
            if attempt == 5:
                raise SystemExit(f"GovInfo did not answer: {exc}")
            time.sleep(2 ** attempt * 5)
    return {}


def with_key(url: str) -> str:
    p = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(p.query, keep_blank_values=True))
    q.setdefault("api_key", KEY)
    return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(q)))


def published(year: int) -> list[str]:
    """Every CFR-<year> package id (titles 1-50), in title/volume order."""
    url = with_key(f"https://api.govinfo.gov/published/{year}-01-01/{year + 1}-06-30"
                   f"?collection=CFR&pageSize=100&offsetMark=*")
    found: set[str] = set()
    for _ in range(80):                       # runaway guard
        data = get(url)
        for pkg in data.get("packages", []):
            m = PKG_RE.match((pkg.get("packageId") or "").strip())
            if m and int(m.group(1)) == year and 1 <= int(m.group(2)) <= 50:
                found.add(m.group(0))
        nxt = data.get("nextPage")
        if not nxt:
            break
        url = with_key(nxt)
    return sorted(found, key=lambda p: tuple(int(x) for x in PKG_RE.match(p).groups()[1:]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=datetime.now(timezone.utc).year)
    ap.add_argument("--out", default="data")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    pub_file = out / f"cfr-{a.year}-published.json"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    first_seen: dict[str, str] = {}
    if pub_file.exists():
        first_seen = json.loads(pub_file.read_text(encoding="utf-8")).get("first_seen", {})

    now = published(a.year)
    if not now and first_seen:
        raise SystemExit("GovInfo returned no volumes although earlier runs found some - not saving.")
    new = [p for p in now if p not in first_seen]
    for p in new:
        first_seen[p] = today

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pub_file.write_text(json.dumps({
        "year": a.year, "checked_utc": stamp, "count": len(now),
        "packages": now, "first_seen": {p: first_seen[p] for p in now},
    }, indent=1), encoding="utf-8")
    (out / f"cfr-{a.year}-new.json").write_text(json.dumps({
        "year": a.year, "checked_utc": stamp, "new": new,
    }, indent=1), encoding="utf-8")
    print(f"CFR {a.year}: {len(now)} volume(s) on GovInfo, {len(new)} new this run"
          + (": " + ", ".join(new) if new else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
