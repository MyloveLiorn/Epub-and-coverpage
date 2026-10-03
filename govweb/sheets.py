"""Writing result tables into a Google Sheet with a service account.

Setup (once): create a service account in Google Cloud with the Google Sheets API enabled,
download its JSON key, and share the sheet with the service account's email as Editor.
Then set GOOGLE_SERVICE_ACCOUNT_JSON (the key file's contents) and GOOGLE_SHEET_ID (the long
id in the sheet's URL). Needs the optional dependency: pip install ".[sheets]".
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
MAX_ROWS = 50_000  # keep each tab well inside Google's cell limit


class SheetsError(RuntimeError):
    pass


def authorized_session(credentials_json: str) -> Any:
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
    except ImportError as exc:
        raise SheetsError('Google Sheets support needs: pip install ".[sheets]"') from exc
    info = json.loads(credentials_json)
    credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return AuthorizedSession(credentials)


def _check(resp: Any, what: str) -> Any:
    if resp.status_code >= 400:
        raise SheetsError(f"Google Sheets {what} failed: HTTP {resp.status_code} {resp.text[:300]}")
    return resp.json() if resp.content else {}


def read_csv(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def upload(session: Any, sheet_id: str, tabs: dict[str, list[list[str]]], formulas: bool = False) -> None:
    """Replace the contents of each named tab (creating missing tabs). Values are written as
    plain text (RAW), so nothing from a website can run as a formula. With ``formulas`` they are
    entered as typed (USER_ENTERED): for tables whose only formulas are our own HYPERLINK cells,
    every other cell having been made safe (a leading apostrophe keeps it text)."""
    base = f"{API}/{sheet_id}"
    meta = _check(session.get(base, params={"fields": "sheets.properties.title"}), "reading the sheet")
    existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
    missing = [title for title in tabs if title not in existing]
    if missing:
        requests_ = [{"addSheet": {"properties": {"title": title}}} for title in missing]
        _check(session.post(f"{base}:batchUpdate", json={"requests": requests_}), "adding tabs")
    for title, rows in tabs.items():
        whole_tab = quote(f"'{title}'", safe="")
        top_left = quote(f"'{title}'!A1", safe="")
        _check(session.post(f"{base}/values/{whole_tab}:clear", json={}), f"clearing {title!r}")
        _check(
            session.put(
                f"{base}/values/{top_left}",
                params={"valueInputOption": "USER_ENTERED" if formulas else "RAW"},
                json={"values": rows[:MAX_ROWS]},
            ),
            f"writing {title!r}",
        )


def tab_titles(session: Any, sheet_id: str) -> list[str]:
    meta = _check(session.get(f"{API}/{sheet_id}", params={"fields": "sheets.properties.title"}), "reading the sheet")
    return [s["properties"]["title"] for s in meta.get("sheets", [])]


def read_tab(session: Any, sheet_id: str, title: str) -> list[list[str]]:
    """A tab's cells as written: formulas (our HYPERLINK cells) as formulas, other cells as text."""
    whole_tab = quote(f"'{title}'", safe="")
    data = _check(
        session.get(f"{API}/{sheet_id}/values/{whole_tab}", params={"valueRenderOption": "FORMULA"}),
        f"reading {title!r}",
    )
    return [[str(cell) for cell in row] for row in data.get("values", [])]
