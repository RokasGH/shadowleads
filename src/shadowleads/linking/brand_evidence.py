"""Brand-level evidence: trademark owners (LINTA) and job-ad employers (Google via Oxylabs).

Both name the company behind a BRAND, which is the operator for single-company brands but may be a
franchisor or group company for chains. Stored in `stg.brand_evidence`, consumed by the fallback
(as weaker, brand-level sources) and by link validation.
"""

from __future__ import annotations

import re
from typing import Any

import duckdb

from shadowleads.linking.resolve import NameResolver
from shadowleads.log import get_logger
from shadowleads.sources.trademarks import TrademarkClient, matching_owners, search_term

log = get_logger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS stg.brand_evidence (
    run_month VARCHAR, place_id VARCHAR, source VARCHAR, brand VARCHAR, company_name VARCHAR,
    ja_kodas BIGINT, detail VARCHAR
)
"""


def busy_targets(
    con: duckdb.DuckDBPyConnection,
    run_month: str,
    *,
    quantile: float,
    only_unresolved: bool,
    limit: int,
) -> list[tuple[str, str, str, str | None, str | None]]:
    """Visibly busy places (>= category quantile of reviews), busiest first."""
    return con.execute(
        f"""
        WITH thr AS (
            SELECT category, quantile_cont(user_rating_count, {quantile:.2f}) AS floor
            FROM core.place_snapshot WHERE run_month = ? AND in_scope GROUP BY category)
        SELECT p.place_id, p.category, p.name, p.street, p.street_number
        FROM core.place_snapshot p
        JOIN thr USING (category)
        JOIN core.place_entity_link l USING (run_month, place_id)
        WHERE p.run_month = ? AND p.in_scope AND NOT p.self_service
          AND p.user_rating_count >= thr.floor
          AND (NOT ? OR l.status <> 'linked' OR l.stage = 'fallback')
        ORDER BY p.user_rating_count DESC
        LIMIT ?
        """,
        [run_month, run_month, only_unresolved, limit],
    ).fetchall()


def _store(
    con: duckdb.DuckDBPyConnection, run_month: str, source: str, rows: list[list[Any]]
) -> None:
    con.execute(DDL)
    con.execute(
        "DELETE FROM stg.brand_evidence WHERE run_month = ? AND source = ?", [run_month, source]
    )
    if rows:
        con.executemany("INSERT INTO stg.brand_evidence VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


def collect_trademarks(
    con: duckdb.DuckDBPyConnection,
    tm: TrademarkClient,
    run_month: str,
    targets: list[tuple[Any, ...]],
) -> int:
    resolver = NameResolver(con)
    rows: list[list[Any]] = []
    for i, (place_id, category, name, *_) in enumerate(targets, 1):
        if not search_term(name):
            continue
        try:
            owners = matching_owners(tm, name, category)
        except Exception as exc:  # one failed lookup must not stop the run
            log.warning("trademark.failed", place=name[:40], error=str(exc)[:120])
            continue
        for o in owners:
            rows.append([run_month, place_id, "trademark", o.mark, o.owner_name,
                         resolver.resolve(o.owner_name), f"LINTA {o.application_no}"])  # fmt: skip
        if i % 50 == 0:
            log.info("trademark.progress", done=i, total=len(targets), owners=len(rows))
    _store(con, run_month, "trademark", rows)
    return len(rows)


def store_job_ads(
    con: duckdb.DuckDBPyConnection, run_month: str, fetched: list[dict[str, Any]],
    names_by_place: dict[str, str],
) -> int:  # fmt: skip
    from shadowleads.sources.oxylabs import employers_from_serp

    resolver = NameResolver(con)
    rows: list[list[Any]] = []
    for item in fetched:
        brand = names_by_place[item["place_id"]]
        for company in sorted(employers_from_serp(item["payload"], brand)):
            if (ja := resolver.resolve(company)) is not None:
                rows.append(
                    [
                        run_month,
                        item["place_id"],
                        "job_ads",
                        brand,
                        company,
                        ja,
                        item["payload"].get("_query"),
                    ]
                )
    _store(con, run_month, "job_ads", rows)
    return len(rows)


def brand_of(name: str) -> str:
    """Brand used in job-ad queries: first two words of the first part of the Google name
    ('7 Fridays Vingis' -> '7 Fridays', 'Grill London' -> 'Grill London')."""
    first = re.split(r"\s*(?:\||/|,|–|—|\s-\s|\()\s*", name)[0]
    return " ".join(first.split()[:2]) or name
