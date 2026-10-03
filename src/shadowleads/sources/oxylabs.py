"""Company-code lookup through Google search results, via the Oxylabs Web Scraper API.

Used for (a) the linking fallback - places the name/address rules could not resolve - and
(b) independent validation of a sample of primary links. Only Google's result titles/snippets are
read (they often quote "Įmonės kodas 305400389" from public company directories); the directory
sites themselves are never crawled.

Budget: the free trial covers ~1,000 Google results in total, so every query is cached on disk and
recorded in `meta.api_ledger`, and a per-run cap applies.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from shadowleads.linking.normalize import fold, name_key
from shadowleads.linking.resolve import company_names_in
from shadowleads.log import get_logger
from shadowleads.sources.websites import extract_codes

log = get_logger(__name__)

SKU = "google_search"


class _Retryable(Exception):
    pass


def build_query(name: str, street: str | None, number: str | None) -> str:
    address = " ".join(p for p in (street, number) if p)
    return " ".join(p for p in (name, address, "Vilnius įmonės kodas") if p)


def codes_from_serp(payload: dict[str, Any], name_hint: str | None = None) -> Counter[str]:
    """Company codes quoted in organic result titles/snippets, counted across results.

    With `name_hint`, only results whose text also mentions every token of the business's
    distinctive name count - snippets routinely quote codes of landlords, neighbours or
    similarly named companies.
    """
    tokens = name_key(name_hint).split() if name_hint else []
    counts: Counter[str] = Counter()
    for result in payload.get("results", []):
        content = result.get("content") or {}
        organic = content.get("results", {}).get("organic", []) if isinstance(content, dict) else []
        for item in organic:
            text = f"{item.get('title', '')} . {item.get('desc', '')}"
            if tokens and not all(t in fold(text).split() for t in tokens):
                continue
            found = {code for kind, code, _ in extract_codes(text) if kind == "company"}
            counts.update(found)
    return counts


class SerpClient:
    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        *,
        username: str,
        password: str,
        api_url: str,
        geo_location: str,
        cache_dir: Path,
        run_month: str,
        budget: int,
    ):
        self.con = con
        self.auth = (username, password)
        self.api_url = api_url
        self.geo = geo_location
        self.cache_dir = cache_dir
        self.run_month = run_month
        self.budget = budget
        self.client = httpx.Client(timeout=150)
        cache_dir.mkdir(parents=True, exist_ok=True)

    def calls_this_run(self) -> int:
        row = self.con.execute(
            "SELECT count(*) FROM meta.api_ledger WHERE provider = 'oxylabs' AND run_month = ?",
            [self.run_month],
        ).fetchone()
        return int(row[0]) if row else 0

    @retry(
        retry=retry_if_exception_type((_Retryable, httpx.TransportError)),
        wait=wait_exponential_jitter(initial=3, max=40),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        resp = self.client.post(self.api_url, auth=self.auth, json=payload)
        if resp.status_code in (429, 502, 503, 504):
            raise _Retryable(str(resp.status_code))
        return resp

    def _key(self, query: str) -> tuple[dict[str, Any], str, Path]:
        payload = {
            "source": "google_search",
            "query": query,
            "geo_location": self.geo,
            "parse": True,
        }
        rh = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
        return payload, rh, self.cache_dir / f"{rh}.json"

    def cached(self, query: str) -> dict[str, Any] | None:
        _, _, path = self._key(query)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def fetch(self, query: str) -> tuple[str, int, dict[str, Any] | None]:
        """Network call only (thread-safe); the caller records it in the ledger."""
        payload, rh, path = self._key(query)
        try:
            resp = self._post(payload)
        except (httpx.HTTPError, _Retryable) as exc:
            log.warning("oxylabs.failed", query=query[:80], error=str(exc)[:120])
            return rh, 0, None
        if resp.status_code != 200:
            return rh, resp.status_code, None
        data = resp.json()
        data.pop("job", None)
        data["_query"] = query
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return rh, 200, data

    def record(self, rh: str, status: int) -> None:
        self.con.execute(
            "INSERT INTO meta.api_ledger VALUES (?, ?, 'oxylabs', ?, ?, ?)",
            [datetime.now(UTC).replace(tzinfo=None), self.run_month, SKU, rh, status],
        )


def run_serp_lookups(
    client: SerpClient,
    targets: list[tuple[str, str, str, str | None, str | None]],
    workers: int = 8,
    query_fn: Any = None,
    fetch_only: bool = False,
) -> list[dict[str, Any]]:
    """targets: (place_id, purpose, name, street, number). Returns one row per (place, code)."""
    queries = {t[0]: (query_fn or build_query)(t[2], t[3], t[4]) for t in targets}
    results: dict[str, dict[str, Any]] = {}
    to_fetch = []
    for place_id, query in queries.items():
        if (cached := client.cached(query)) is not None:
            results[place_id] = cached
        else:
            to_fetch.append(place_id)
    room = max(0, client.budget - client.calls_this_run())
    if len(to_fetch) > room:
        log.warning("oxylabs.budget_limits_targets", wanted=len(to_fetch), allowed=room)
        to_fetch = to_fetch[:room]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(client.fetch, queries[pid]): pid for pid in to_fetch}
        for i, fut in enumerate(as_completed(futures), 1):
            rh, status, data = fut.result()
            client.record(rh, status)
            if data is not None:
                results[futures[fut]] = data
            if i % 25 == 0:
                log.info("oxylabs.progress", done=i, total=len(to_fetch))

    if fetch_only:
        return [{"place_id": pid, "payload": results[pid]} for pid in queries if pid in results]
    rows: list[dict[str, Any]] = []
    for place_id, purpose, name, *_ in targets:
        if place_id not in results:
            continue
        query = queries[place_id]
        named = codes_from_serp(results[place_id], name)
        base = {"place_id": place_id, "purpose": purpose, "query": query}
        rows.append({**base, "code": None, "hits": 0, "hits_named": 0})
        rows += [
            {**base, "code": c, "hits": n, "hits_named": named.get(c, 0)}
            for c, n in codes_from_serp(results[place_id]).items()
        ]
    return rows


def job_ad_query(brand: str) -> str:
    return f'"{brand}" Vilnius darbo skelbimas'


def employers_from_serp(payload: dict[str, Any], brand: str) -> set[str]:
    """Company names in job-ad results that also mention the brand ('UAB „Kauno loftas“ ...
    restorane "Grill London"'). Employers of a brand, not necessarily of this exact venue."""
    tokens = name_key(brand).split()
    names: set[str] = set()
    for result in payload.get("results", []):
        content = result.get("content") or {}
        organic = content.get("results", {}).get("organic", []) if isinstance(content, dict) else []
        for item in organic:
            text = f"{item.get('title', '')} . {item.get('desc', '')}"
            if tokens and all(t in fold(text).split() for t in tokens):
                names |= company_names_in(text)
    return names
