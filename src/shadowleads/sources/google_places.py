"""Visible activity: Google Places API (New) Nearby Search over Vilnius.

Nearby Search returns at most 20 places per call and has no pagination, so the city is swept with
an adaptive quadtree: a square cell is queried with its circumscribed circle; if the answer is
saturated (20 results) the cell is split into 4. Leaves with < 20 results are complete.

Cost control: every response is cached on disk by request hash (re-running transforms never re-bills)
and every billable call is written to `meta.api_ledger`; the sweep stops at the monthly budget.

Terms-of-service note (EEA Maps terms): only `place_id` may be stored indefinitely. Raw payloads are
kept under `data/raw/google/<month>/` for the monthly run and are meant to be purged after 30 days
(`shadowleads purge-google`); nothing from this directory is committed to git.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from shadowleads.categories import CATEGORIES, category_for_primary_type
from shadowleads.log import get_logger

log = get_logger(__name__)

NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
SKU = "nearby_search_enterprise"
MAX_RESULTS = 20
MIN_RADIUS_M = 75.0

FIELD_MASK = ",".join(
    f"places.{f}"
    for f in (
        "id", "displayName", "formattedAddress", "addressComponents", "location", "types",
        "primaryType", "businessStatus", "rating", "userRatingCount", "regularOpeningHours",
        "priceRange", "priceLevel", "websiteUri", "nationalPhoneNumber", "googleMapsUri",
    )
)  # fmt: skip

M_PER_DEG_LAT = 111_320.0


class BudgetExhaustedError(RuntimeError):
    pass


class _Retryable(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Cell:
    """Axis-aligned square in a local metric frame, stored as centre + half side in metres."""

    lat: float
    lng: float
    half_side_m: float
    depth: int

    @property
    def radius_m(self) -> float:
        return self.half_side_m * math.sqrt(2)

    def split(self, k: int = 2) -> list[Cell]:
        """k x k grid of equal child squares."""
        child_half = self.half_side_m / k
        dlat = 1 / M_PER_DEG_LAT
        dlng = 1 / (M_PER_DEG_LAT * math.cos(math.radians(self.lat)))
        offsets = [-self.half_side_m + child_half * (2 * i + 1) for i in range(k)]
        return [
            Cell(self.lat + oy * dlat, self.lng + ox * dlng, child_half, self.depth + 1)
            for oy in offsets
            for ox in offsets
        ]

    def corners(self) -> list[tuple[float, float]]:
        dlat = self.half_side_m / M_PER_DEG_LAT
        dlng = self.half_side_m / (M_PER_DEG_LAT * math.cos(math.radians(self.lat)))
        return [(self.lat + sy * dlat, self.lng + sx * dlng) for sy in (-1, 1) for sx in (-1, 1)]


def root_cell(bbox: tuple[float, float, float, float]) -> Cell:
    south, west, north, east = bbox
    lat, lng = (south + north) / 2, (west + east) / 2
    height = (north - south) * M_PER_DEG_LAT
    width = (east - west) * M_PER_DEG_LAT * math.cos(math.radians(lat))
    return Cell(lat, lng, max(height, width) / 2, 0)


def request_body(cell: Cell, primary_types: list[str]) -> dict[str, Any]:
    return {
        "includedPrimaryTypes": primary_types,
        "maxResultCount": MAX_RESULTS,
        # DISTANCE ranking makes a saturated answer informative: every matching place closer
        # than the 20th result has been seen, so child cells inside that disc can be skipped.
        "rankPreference": "DISTANCE",
        "languageCode": "lt",
        "regionCode": "LT",
        "locationRestriction": {
            "circle": {
                "center": {"latitude": round(cell.lat, 6), "longitude": round(cell.lng, 6)},
                "radius": round(min(cell.radius_m, 50_000.0), 1),
            }
        },
    }


def request_hash(body: dict[str, Any]) -> str:
    blob = json.dumps({"url": NEARBY_URL, "mask": FIELD_MASK, "body": body}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


class PlacesCollector:
    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        api_key: str,
        cache_dir: Path,
        run_month: str,
        budget: int,
    ):
        self.con = con
        self.api_key = api_key
        self.cache_dir = cache_dir
        self.run_month = run_month
        self.budget = budget
        self.client = httpx.Client(timeout=30)
        cache_dir.mkdir(parents=True, exist_ok=True)

    def calls_this_billing_month(self) -> int:
        row = self.con.execute(
            "SELECT count(*) FROM meta.api_ledger WHERE provider = 'google' AND sku = ? "
            "AND date_trunc('month', called_at) = date_trunc('month', current_date)",
            [SKU],
        ).fetchone()
        return int(row[0]) if row else 0

    @retry(
        retry=retry_if_exception_type((_Retryable, httpx.TransportError)),
        wait=wait_exponential_jitter(initial=2, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _post(self, body: dict[str, Any]) -> httpx.Response:
        resp = self.client.post(
            NEARBY_URL,
            json=body,
            headers={"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": FIELD_MASK},
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _Retryable(str(resp.status_code))
        return resp

    def search(self, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Cached Nearby Search. Returns (request_hash, response json)."""
        rh = request_hash(body)
        path = self.cache_dir / f"{rh}.json"
        if path.exists():
            return rh, json.loads(path.read_text(encoding="utf-8"))
        if self.calls_this_billing_month() >= self.budget:
            raise BudgetExhaustedError(f"Google {SKU} budget of {self.budget} calls reached")
        resp = self._post(body)
        self.con.execute(
            "INSERT INTO meta.api_ledger VALUES (?, ?, 'google', ?, ?, ?)",
            [datetime.now(UTC).replace(tzinfo=None), self.run_month, SKU, rh, resp.status_code],
        )
        resp.raise_for_status()
        payload = resp.json()
        payload["_request"] = body
        payload["_fetched_at"] = datetime.now(UTC).isoformat()
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return rh, payload

    def sweep(
        self, bbox: tuple[float, float, float, float], primary_types: list[str]
    ) -> list[dict[str, Any]]:
        """Adaptive quadtree sweep. Returns the manifest: one row per queried cell."""
        manifest: list[dict[str, Any]] = []
        stack = [root_cell(bbox)]
        while stack:
            cell = stack.pop()
            body = request_body(cell, primary_types)
            try:
                rh, payload = self.search(body)
            except BudgetExhaustedError:
                log.error("google.budget_exhausted", pending_cells=len(stack) + 1)
                manifest.append(_manifest_row(cell, None, 0, status="budget_exhausted"))
                manifest.extend(_manifest_row(c, None, 0, status="budget_exhausted") for c in stack)
                break
            n = len(payload.get("places", []))
            saturated = n >= MAX_RESULTS
            can_split = cell.radius_m / 2 >= MIN_RADIUS_M
            if saturated and can_split:
                status = "split"
                covered = _covered_radius(cell, payload.get("places", []))
                for child in cell.split(split_factor(cell, covered)):
                    inside = all(
                        haversine_m(cell.lat, cell.lng, la, ln) < covered
                        for la, ln in child.corners()
                    )
                    if inside:  # every place in this child square was already returned
                        manifest.append(_manifest_row(child, None, 0, status="covered_by_parent"))
                    else:
                        stack.append(child)
            else:
                status = "truncated" if saturated else "complete"
            manifest.append(_manifest_row(cell, rh, n, status=status))
            log.info(
                "google.cell", depth=cell.depth, radius=round(cell.radius_m), n=n, status=status
            )
        return manifest


def split_factor(cell: Cell, covered_m: float, target_per_cell: int = 12) -> int:
    """How finely to split a saturated cell, from the density implied by the 20 nearest places.

    Skipping intermediate quadtree levels saves the calls that would only re-saturate.
    """
    if covered_m <= 0:
        return 2
    density = MAX_RESULTS / (math.pi * covered_m**2)
    expected = density * (2 * cell.half_side_m) ** 2
    k = math.ceil(math.sqrt(expected / target_per_cell))
    # never make children smaller than the minimum query radius
    k_max = max(2, int(cell.radius_m / MIN_RADIUS_M))
    return max(2, min(k, 6, k_max))


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


def _covered_radius(cell: Cell, places: list[dict[str, Any]]) -> float:
    """Radius around the cell centre within which all matching places were returned."""
    dists = [
        haversine_m(cell.lat, cell.lng, p["location"]["latitude"], p["location"]["longitude"])
        for p in places
        if p.get("location")
    ]
    return min(max(dists), cell.radius_m) if dists else 0.0


def _manifest_row(cell: Cell, rh: str | None, n: int, *, status: str) -> dict[str, Any]:
    return {
        "lat": cell.lat, "lng": cell.lng, "radius_m": cell.radius_m, "depth": cell.depth,
        "request_hash": rh, "n_results": n, "status": status,
    }  # fmt: skip


def weekly_open_hours(opening: dict[str, Any] | None) -> float | None:
    """Sum of opening hours over a week from `regularOpeningHours.periods`.

    A single period with `open` and no `close` means open 24/7.
    """
    if not opening or not opening.get("periods"):
        return None
    total = 0.0
    for p in opening["periods"]:
        o = p.get("open")
        c = p.get("close")
        if o is None:
            continue
        if c is None:
            return 168.0
        start = o.get("day", 0) * 1440 + o.get("hour", 0) * 60 + o.get("minute", 0)
        end = c.get("day", 0) * 1440 + c.get("hour", 0) * 60 + c.get("minute", 0)
        if end <= start:
            end += 7 * 1440
        total += (end - start) / 60
    return round(min(total, 168.0), 2)


def _component(components: list[dict[str, Any]], kind: str, short: bool = True) -> str | None:
    for comp in components or []:
        if kind in comp.get("types", []):
            return comp.get("shortText" if short else "longText")
    return None


def _price(p: dict[str, Any] | None, key: str) -> float | None:
    if not p or key not in p:
        return None
    money = p[key]
    return float(money.get("units", 0)) + money.get("nanos", 0) / 1e9


def flatten_place(place: dict[str, Any], rh: str, fetched_at: str) -> dict[str, Any]:
    comps = place.get("addressComponents", [])
    loc = place.get("location", {})
    ptype = place.get("primaryType")
    return {
        "place_id": place["id"],
        "name": (place.get("displayName") or {}).get("text"),
        "primary_type": ptype,
        "category": category_for_primary_type(ptype),
        "types": place.get("types", []),
        "formatted_address": place.get("formattedAddress"),
        "street": _component(comps, "route"),
        "street_number": _component(comps, "street_number"),
        "postal_code": _component(comps, "postal_code"),
        "locality": _component(comps, "locality"),
        "lat": loc.get("latitude"),
        "lng": loc.get("longitude"),
        "business_status": place.get("businessStatus"),
        "rating": place.get("rating"),
        "user_rating_count": place.get("userRatingCount"),
        "weekly_open_hours": weekly_open_hours(place.get("regularOpeningHours")),
        "opening_hours_text": (place.get("regularOpeningHours") or {}).get("weekdayDescriptions"),
        "price_from": _price(place.get("priceRange"), "startPrice"),
        "price_to": _price(place.get("priceRange"), "endPrice"),
        "price_level": place.get("priceLevel"),
        "website": place.get("websiteUri"),
        "phone": place.get("nationalPhoneNumber"),
        "maps_uri": place.get("googleMapsUri"),
        "request_hash": rh,
        "fetched_at": fetched_at,
    }


def all_primary_types() -> list[str]:
    return sorted({t for c in CATEGORIES.values() for t in c.google_primary_types})


def load_snapshot(
    con: duckdb.DuckDBPyConnection, cache_dir: Path, manifest: list[dict[str, Any]], run_month: str
) -> int:
    """Stage one row per (run_month, place_id) from the cached responses of this sweep."""
    rows: dict[str, dict[str, Any]] = {}
    for m in manifest:
        if not m["request_hash"]:
            continue
        payload = json.loads((cache_dir / f"{m['request_hash']}.json").read_text(encoding="utf-8"))
        for place in payload.get("places", []):
            rows.setdefault(
                place["id"], flatten_place(place, m["request_hash"], payload["_fetched_at"])
            )

    out = cache_dir.parent / "places.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for r in rows.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    man = cache_dir.parent / "manifest.jsonl"
    with man.open("w", encoding="utf-8") as f:
        for m in manifest:
            f.write(json.dumps(m) + "\n")

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS stg.google_place (
            run_month VARCHAR, place_id VARCHAR, name VARCHAR, primary_type VARCHAR,
            category VARCHAR, types VARCHAR[], formatted_address VARCHAR, street VARCHAR,
            street_number VARCHAR, postal_code VARCHAR, locality VARCHAR, lat DOUBLE, lng DOUBLE,
            business_status VARCHAR, rating DOUBLE, user_rating_count INTEGER,
            weekly_open_hours DOUBLE, opening_hours_text VARCHAR[], price_from DOUBLE,
            price_to DOUBLE, price_level VARCHAR, website VARCHAR, phone VARCHAR, maps_uri VARCHAR,
            request_hash VARCHAR, fetched_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS stg.google_sweep (
            run_month VARCHAR, lat DOUBLE, lng DOUBLE, radius_m DOUBLE, depth INTEGER,
            request_hash VARCHAR, n_results INTEGER, status VARCHAR
        );
        """
    )
    con.execute("DELETE FROM stg.google_place WHERE run_month = ?", [run_month])
    con.execute("DELETE FROM stg.google_sweep WHERE run_month = ?", [run_month])
    if rows:
        con.execute(
            f"""
            INSERT INTO stg.google_place
            SELECT '{run_month}', place_id, name, primary_type, category, types, formatted_address,
                   street, street_number, postal_code, locality, lat, lng, business_status, rating,
                   user_rating_count, weekly_open_hours, opening_hours_text, price_from, price_to,
                   price_level, website, phone, maps_uri, request_hash,
                   CAST(fetched_at AS TIMESTAMPTZ)::TIMESTAMP
            FROM read_json('{out}', format='newline_delimited', columns={{
                'place_id':'VARCHAR','name':'VARCHAR','primary_type':'VARCHAR','category':'VARCHAR',
                'types':'VARCHAR[]','formatted_address':'VARCHAR','street':'VARCHAR',
                'street_number':'VARCHAR','postal_code':'VARCHAR','locality':'VARCHAR',
                'lat':'DOUBLE','lng':'DOUBLE','business_status':'VARCHAR','rating':'DOUBLE',
                'user_rating_count':'INTEGER','weekly_open_hours':'DOUBLE',
                'opening_hours_text':'VARCHAR[]','price_from':'DOUBLE','price_to':'DOUBLE',
                'price_level':'VARCHAR','website':'VARCHAR','phone':'VARCHAR','maps_uri':'VARCHAR',
                'request_hash':'VARCHAR','fetched_at':'VARCHAR'}})
            """
        )
    con.execute(
        f"""INSERT INTO stg.google_sweep SELECT '{run_month}', * FROM read_json('{man}',
            format='newline_delimited', columns={{'lat':'DOUBLE','lng':'DOUBLE','radius_m':'DOUBLE',
            'depth':'INTEGER','request_hash':'VARCHAR','n_results':'INTEGER','status':'VARCHAR'}})"""
    )
    log.info("google.snapshot_loaded", places=len(rows), cells=len(manifest))
    return len(rows)
