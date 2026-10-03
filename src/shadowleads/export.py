"""Exports.

* `export_run`      - local analyst outputs with real names (output/<month>/, never committed).
* `export_example`  - the public example committed to git (examples/<month>/): pseudonymised.
* `load_example`    - rebuilds a warehouse from the example for the offline demo.

Pseudonymisation (public example):
  - company codes, names, place ids/names -> keyed HMAC pseudonyms (a plain hash of a 9-digit code
    could be brute-forced against the public register); fake codes start with 9, real JAR codes
    never do.
  - quasi-identifiers coarsened: review counts bucketed, declared amounts rounded, coordinates to
    ~1 km, street addresses / websites / Google URLs dropped - exact Sodra headcounts or tax
    amounts could otherwise be looked up in the public datasets.
  - exact scores/gaps are rounded (gap + peer median would reveal the exact tax figure) and
    per-entity official time series (Sodra months, VMI years, revenue) are published only for the
    negative control - monthly Sodra patterns could be matched against Sodra's own open data.
  - one NEGATIVE CONTROL (a large, compliant, not-flagged entity) is kept in clear to show the full
    lineage without pointing at anyone.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

import duckdb

from shadowleads.log import get_logger

log = get_logger(__name__)


def _hmac(key: bytes, value: object) -> str:
    return hmac.new(key, str(value).encode(), hashlib.sha256).hexdigest()


def fake_code(key: bytes, ja: int) -> int:
    return 900_000_000 + int(_hmac(key, ja)[:12], 16) % 100_000_000


def export_run(con: duckdb.DuckDBPyConnection, run_month: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "leads.csv"
    con.execute(
        f"""COPY (SELECT * EXCLUDE (place_ids) FROM mart.lead
                 WHERE run_month = '{run_month}' ORDER BY priority_rank)
            TO '{path}' (HEADER)"""
    )
    return path


def negative_control(con: duckdb.DuckDBPyConnection, run_month: str) -> int | None:
    row = con.execute(
        """SELECT ja_kodas FROM mart.lead
           WHERE run_month = ? AND tier = 'C_not_flagged' AND hold_reason = 'taxes in line with peers'
             AND NOT owner_operated
           ORDER BY reviews_total DESC LIMIT 1""",
        [run_month],
    ).fetchone()
    return row[0] if row else None


def export_example(
    con: duckdb.DuckDBPyConnection, run_month: str, out_dir: Path, key: bytes
) -> list[Path]:
    """Write pseudonymised Parquet tables + a leads.csv for the public repo."""
    out_dir.mkdir(parents=True, exist_ok=True)
    control = negative_control(con, run_month) or -1
    con.create_function(
        "pseudo_code", lambda ja: ja if ja == control else fake_code(key, ja), ["BIGINT"], "BIGINT"
    )
    con.create_function("pseudo", lambda v: "x" + _hmac(key, v)[:10], ["VARCHAR"], "VARCHAR")
    m = run_month
    keep = f"ja_kodas = {control}"
    bucket = (
        "CASE WHEN {c} IS NULL THEN NULL WHEN {c} < 100 THEN round({c} / 10) * 10 "
        "WHEN {c} < 1000 THEN round({c} / 50) * 50 ELSE round({c} / 250) * 250 END"
    )
    money = "CASE WHEN {c} IS NULL THEN NULL WHEN abs({c}) < 10000 THEN round({c} / 100) * 100 ELSE round({c} / 1000) * 1000 END"

    def name(col: str, label: str) -> str:
        return (
            f"CASE WHEN {keep} THEN {col} ELSE '{label} ' || pseudo(CAST(ja_kodas AS VARCHAR)) END"
        )

    queries = {
        "lead": f"""
            SELECT * REPLACE (
                pseudo_code(ja_kodas) AS ja_kodas,
                {name("legal_name", "Entity")} AS legal_name,
                CASE WHEN {keep} THEN place_names ELSE list_transform(place_names, x -> 'Place ' || pseudo(x)) END AS place_names,
                CASE WHEN {keep} THEN place_ids ELSE list_transform(place_ids, x -> pseudo(x)) END AS place_ids,
                {bucket.format(c="reviews_total")} AS reviews_total,
                round(reviews_per_year, -1) AS reviews_per_year,
                {money.format(c="taxes_paid")} AS taxes_paid,
                {money.format(c="taxes_ytd")} AS taxes_ytd,
                {money.format(c="contributions")} AS contributions,
                {money.format(c="revenue")} AS revenue,
                round(insured_avg * 2) / 2 AS insured_avg,
                round(insured_avg_t12m * 2) / 2 AS insured_avg_t12m,
                round(rating_weighted * 2) / 2 AS rating_weighted,
                -- exact gaps + peer medians would let anyone recompute the exact declared figure
                round(score, 1) AS score, round(gap_taxes, 1) AS gap_taxes,
                round(gap_payroll, 1) AS gap_payroll, round(gap_revenue, 1) AS gap_revenue,
                {money.format(c="peer_median_taxes")} AS peer_median_taxes,
                {money.format(c="peer_median_contributions")} AS peer_median_contributions,
                {money.format(c="peer_median_revenue")} AS peer_median_revenue,
                round(reviews_per_1k_taxes, 0) AS reviews_per_1k_taxes,
                round(reviews_per_1k_revenue, 0) AS reviews_per_1k_revenue,
                round(insured_per_100_reviews, 0) AS insured_per_100_reviews,
                round(weekly_open_hours_max, -1) AS weekly_open_hours_max,
                round(entity_age_months, -1) AS entity_age_months)
            FROM mart.lead WHERE run_month = '{m}'""",
        "entity_activity": f"""
            SELECT run_month, pseudo_code(ja_kodas) AS ja_kodas, {name("legal_name", "Entity")} AS legal_name,
                   legal_form, owner_operated, main_category, n_places,
                   {bucket.format(c="reviews_total")} AS reviews_total,
                   round(rating_weighted * 2) / 2 AS rating_weighted,
                   round(weekly_open_hours_max, -1) AS weekly_open_hours_max, vat_registered, has_vmi_record, has_sodra_record,
                   {money.format(c="taxes_prev_year")} AS taxes_prev_year,
                   round(insured_avg_prev_year * 2) / 2 AS insured_avg_prev_year,
                   {money.format(c="revenue_latest")} AS revenue_latest, revenue_fy, revenue_is_stale,
                   multi_site, shared_premises, new_entity, all_links_usable, link_methods,
                   validation_statuses, tax_year, sodra_as_of
            FROM mart.entity_activity WHERE run_month = '{m}'""",
        "place_snapshot": f"""
            SELECT p.run_month,
                   CASE WHEN v.ja_kodas = {control} THEN p.place_id ELSE pseudo(p.place_id) END AS place_id,
                   CASE WHEN v.ja_kodas = {control} THEN p.name ELSE 'Place ' || pseudo(p.name) END AS name,
                   p.category, p.primary_type, p.in_scope, p.out_of_scope_reason, p.self_service,
                   p.business_status, round(p.rating * 2) / 2 AS rating, {bucket.format(c="p.user_rating_count")} AS user_rating_count,
                   p.weekly_open_hours, p.price_from, p.price_to,
                   round(p.lat, 2) AS lat, round(p.lng, 2) AS lng,
                   CASE WHEN v.ja_kodas = {control} THEN p.formatted_address END AS formatted_address,
                   CASE WHEN v.ja_kodas = {control} THEN p.maps_uri END AS maps_uri,
                   NULL::VARCHAR AS website, p.fetched_at
            FROM core.place_snapshot p
            LEFT JOIN core.link_validation v USING (run_month, place_id)
            WHERE p.run_month = '{m}'""",
        "place_entity_link": f"""
            SELECT l.run_month,
                   CASE WHEN l.ja_kodas = {control} THEN l.place_id ELSE pseudo(l.place_id) END AS place_id,
                   l.status, pseudo_code(l.ja_kodas) AS ja_kodas, l.method, l.confidence, l.stage,
                   -- analyst free text may quote real company names
                   CASE WHEN l.stage = 'override' THEN 'analyst override' ELSE l.reason END AS reason
            FROM core.place_entity_link l WHERE l.run_month = '{m}'""",
        "link_validation": f"""
            SELECT * REPLACE (
                CASE WHEN ja_kodas = {control} THEN place_id ELSE pseudo(place_id) END AS place_id,
                pseudo_code(ja_kodas) AS ja_kodas, NULL::BIGINT AS prev_ja_kodas,
                NULL::VARCHAR AS agreement_detail)
            FROM core.link_validation WHERE run_month = '{m}'""",
        "vmi_taxes": f"""
            SELECT pseudo_code(t.ja_kodas) AS ja_kodas, t.legal_form, t.year, t.through_month,
                   {money.format(c="t.taxes_paid")} AS taxes_paid, t.updated_on, t.fetch_id
            FROM stg.vmi_taxes t WHERE t.ja_kodas = {control}""",
        "sodra_monthly": f"""
            SELECT pseudo_code(s.ja_kodas) AS ja_kodas, s.month, s.evrk,
                   CASE WHEN s.ja_kodas = {control} THEN s.num_insured ELSE round(s.num_insured / 2) * 2 END AS num_insured,
                   {money.format(c="s.contributions")} AS contributions, NULL::DOUBLE AS avg_wage, s.fetch_id
            FROM stg.sodra_monthly s WHERE s.ja_kodas = {control}""",
        "rc_revenue": f"""
            SELECT pseudo_code(r.ja_kodas) AS ja_kodas, r.fiscal_year,
                   {money.format(c="r.revenue")} AS revenue, r.filed_on, r.fetch_id
            FROM stg.rc_revenue r WHERE r.ja_kodas = {control}""",
        "google_sweep": f"SELECT * EXCLUDE (request_hash) FROM stg.google_sweep WHERE run_month = '{m}'",
        "dq_result": f"SELECT * FROM meta.dq_result WHERE run_month = '{m}'",
        "source_fetch": "SELECT fetch_id, source, url, fetched_at, sha256, bytes, row_count, run_month FROM meta.source_fetch",
    }
    paths = []
    for table, sql in queries.items():
        path = out_dir / f"{table}.parquet"
        con.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet, COMPRESSION zstd)")
        paths.append(path)
    csv = out_dir / "leads.csv"
    con.execute(
        f"""COPY (SELECT priority_rank, tier, legal_name, ja_kodas, main_category, n_places, reviews_total,
                         rating_weighted, taxes_paid, peer_median_taxes, insured_avg, revenue, revenue_fy,
                         score, gap_taxes, gap_payroll, gap_revenue, sig_near_zero_declared,
                         sig_staffing_floor, sig_vat_gap, hold_reason, link_methods, validation_statuses
                  FROM read_parquet('{out_dir / "lead.parquet"}') ORDER BY priority_rank)
            TO '{csv}' (HEADER)"""
    )
    log.info("example.exported", dir=str(out_dir), negative_control=control)
    return [*paths, csv]


TABLE_SCHEMA = {
    "lead": "mart.lead",
    "entity_activity": "mart.entity_activity",
    "place_snapshot": "core.place_snapshot",
    "place_entity_link": "core.place_entity_link",
    "link_validation": "core.link_validation",
    "vmi_taxes": "stg.vmi_taxes",
    "sodra_monthly": "stg.sodra_monthly",
    "rc_revenue": "stg.rc_revenue",
    "google_sweep": "stg.google_sweep",
    "dq_result": "meta.dq_result",
    "source_fetch": "meta.source_fetch",
}


def load_example(con: duckdb.DuckDBPyConnection, example_dir: Path) -> None:
    """Offline demo: build the warehouse tables the app reads from committed Parquet files."""
    for month_dir in sorted(p for p in example_dir.iterdir() if p.is_dir()):
        for name, table in TABLE_SCHEMA.items():
            path = month_dir / f"{name}.parquet"
            if not path.exists():
                continue
            exists = con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema || '.' || table_name = ?",
                [table],
            ).fetchone()
            if exists and exists[0] and table not in {"meta.dq_result", "meta.source_fetch"}:
                con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM read_parquet('{path}')")
            else:
                con.execute(f"DROP TABLE IF EXISTS {table}")
                con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_parquet('{path}')")
    log.info("example.loaded", dir=str(example_dir))
