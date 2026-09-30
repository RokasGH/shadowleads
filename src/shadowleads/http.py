"""Shared HTTP client: polite throttling per host, retries with backoff, block-page detection.

get.data.gov.lt sometimes answers with a firewall "URL blocked" HTML page and HTTP 200, and
data.gov.lt returns 429 after a burst, so status codes alone cannot be trusted.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from shadowleads.log import get_logger

log = get_logger(__name__)


class RetryableHTTPError(Exception):
    """Transient failure (429, 5xx, firewall page) - safe to retry."""


class BlockedResponseError(RetryableHTTPError):
    """Got an HTML page where data was expected."""


_BLOCK_MARKERS = (b"URL blocked", b"FortiGuard", b"Web Page Blocked", b"Access denied")


def looks_blocked(content: bytes, content_type: str, expect: str | None) -> bool:
    """True when a response that should be data is actually an HTML block/error page."""
    if expect is None:
        return False
    head = content[:4096]
    if any(m in head for m in _BLOCK_MARKERS):
        return True
    is_html = "text/html" in content_type or head.lstrip()[:15].lower().startswith(
        (b"<!doctype html", b"<html")
    )
    return expect in {"json", "csv", "zip"} and is_html


class PoliteClient:
    """httpx.Client wrapper enforcing a minimum interval between requests to the same host."""

    def __init__(self, user_agent: str, *, min_interval: float = 1.0, timeout: float = 120.0):
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(timeout, connect=20.0),
            follow_redirects=True,
        )
        self._min_interval = min_interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        with self._lock:
            wait = self._last.get(host, 0.0) + self._min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()

    @retry(
        retry=retry_if_exception_type((RetryableHTTPError, httpx.TransportError)),
        wait=wait_exponential_jitter(initial=3, max=60),
        stop=stop_after_attempt(6),
        reraise=True,
    )
    def request(
        self, method: str, url: str, *, expect: str | None = None, **kwargs: object
    ) -> httpx.Response:
        self._throttle(url)
        resp = self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
        if resp.status_code == 429 or resp.status_code >= 500:
            log.warning("http.retryable_status", url=url[:160], status=resp.status_code)
            raise RetryableHTTPError(f"{resp.status_code} for {url}")
        resp.raise_for_status()
        if looks_blocked(resp.content, resp.headers.get("content-type", ""), expect):
            log.warning("http.block_page", url=url[:160])
            raise BlockedResponseError(f"block page instead of {expect} for {url}")
        return resp

    def get(self, url: str, *, expect: str | None = None, **kwargs: object) -> httpx.Response:
        return self.request("GET", url, expect=expect, **kwargs)

    def post(self, url: str, *, expect: str | None = None, **kwargs: object) -> httpx.Response:
        return self.request("POST", url, expect=expect, **kwargs)

    def download(self, url: str, dest: Path, *, expect: str | None = None) -> Path:
        """Stream a (large) file to disk atomically, then validate it is not a block page."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        self._download(url, tmp, expect)
        tmp.replace(dest)
        return dest

    @retry(
        retry=retry_if_exception_type((RetryableHTTPError, httpx.TransportError)),
        wait=wait_exponential_jitter(initial=5, max=90),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _download(self, url: str, tmp: Path, expect: str | None) -> None:
        self._throttle(url)
        with self._client.stream("GET", url) as resp:
            if resp.status_code == 429 or resp.status_code >= 500:
                raise RetryableHTTPError(f"{resp.status_code} for {url}")
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            with tmp.open("wb") as f:
                for chunk in resp.iter_bytes(1 << 20):
                    f.write(chunk)
        with tmp.open("rb") as f:
            head = f.read(4096)
        if looks_blocked(head, ctype, expect):
            raise BlockedResponseError(f"block page instead of {expect} for {url}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
