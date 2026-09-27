"""A small JSON HTTP client with retries and per-host politeness delays."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from govbooks import __version__

USER_AGENT = f"govbooks/{__version__} (+https://github.com/MyloveLiorn/Epub-and-coverpage)"


class HttpError(RuntimeError):
    """A failed request: an error status, a network failure (status None) or a non-JSON reply."""

    def __init__(self, status: int | None, url: str, detail: str):
        prefix = f"HTTP {status}" if status else "Request failed"
        super().__init__(f"{prefix} for {url.split('?')[0]}: {detail[:300]}")
        self.status = status
        self.url = url
        self.detail = detail


class Http:
    """JSON GET/POST with retry on 429/5xx and a minimum delay between calls to the same host."""

    def __init__(self, min_interval: float = 1.0, timeout: float = 30.0):
        self.min_interval = min_interval
        self.timeout = timeout
        self._last_call: dict[str, float] = {}
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        retry = Retry(
            total=4,
            backoff_factor=2,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=None,
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        wait = self._last_call.get(host, 0.0) + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call[host] = time.monotonic()

    def _send(self, method: str, url: str, **kwargs: Any) -> Any:
        self._throttle(url)
        try:
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise HttpError(None, url, str(exc)) from exc
        if resp.status_code >= 400:
            raise HttpError(resp.status_code, url, resp.text)
        try:
            return resp.json()
        except ValueError as exc:
            raise HttpError(resp.status_code, url, f"reply was not JSON: {resp.text[:100]!r}") from exc

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> Any:
        return self._send("GET", url, params=params, headers=headers)

    def post_json(
        self,
        url: str,
        json: Any = None,
        data: dict | None = None,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> Any:
        return self._send("POST", url, json=json, data=data, params=params, headers=headers)
