"""Exports.

* `export_run`     - analyst CSV of the lead list (output/<month>/leads.csv).
* `export_example` - the committed example (examples/<month>/): the tables the analyst app needs,
                     restricted to entities in the lead list, as Parquet. Company data is public
                     register information, so names and codes are kept as they are. Raw Google API
                     payloads are not committed (see DECISIONS.md on Maps terms).
* `load_example`   - rebuilds a warehouse from the example for the offline demo.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from shadowleads.log import get_logger

log = get_logger(__name__)

_LEADS = "ja_kodas IN (SELECT ja_kodas FROM mart.lead WHERE run_month = '{m}')"

# example file name -> (warehouse table, filter that keeps it to this run's entities)
EXAMPLE_TABLES: dict[str, tuple[str, str]] = {
    "lead": ("mart.lead", "run_month = '{m}'"),
    "entity_activity": ("mart.entity_activity", "run_month = '{m}'"),
    "entity": ("core.entity", _LEADS),
    "place_snapshot": ("core.place_snapshot", "run_month = '{m}'"),
    "place_entity_link": ("core.place_entity_link", "run_month = '{m}'"),
    "link_validation": ("core.link_validation", "run_month = '{m}'"),
    "match_candidate": ("core.match_candidate", "run_month = '{m}'"),
    "link_agreement": ("core.link_agreement", "run_month = '{m}'"),
    "entity_branch": ("core.entity_branch", "in_vilnius AND " + _LEADS),
    "vmi_taxes": ("stg.vmi_taxes", _LEADS),
    "sodra_monthly": ("stg.sodra_monthly", _LEADS),
    "rc_revenue": ("stg.rc_revenue", _LEADS),
    "vmvt_premises": ("stg.vmvt_premises", _LEADS),
    "website_code": ("stg.website_code", "run_month = '{m}' AND code IS NOT NULL"),
    "serp_code": ("stg.serp_code", "run_month = '{m}' AND code IS NOT NULL"),
    "brand_evidence": ("stg.brand_evidence", "run_month = '{m}'"),
    "google_sweep": ("stg.google_sweep", "run_month = '{m}'"),
    "dq_result": ("meta.dq_result", "run_month = '{m}'"),
    "source_fetch": ("meta.source_fetch", "true"),
}


def export_run(con: duckdb.DuckDBPyConnection, run_month: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "leads.csv"
    con.execute(
        f"""COPY (SELECT * EXCLUDE (place_ids) FROM mart.lead
                 WHERE run_month = '{run_month}' ORDER BY priority_rank)
            TO '{path}' (HEADER)"""
    )
    return path


def export_example(con: duckdb.DuckDBPyConnection, run_month: str, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, (table, where) in EXAMPLE_TABLES.items():
        path = out_dir / f"{name}.parquet"
        select = "SELECT * EXCLUDE (path)" if table == "meta.source_fetch" else "SELECT *"
        con.execute(
            f"COPY ({select} FROM {table} WHERE {where.format(m=run_month)}) "
            f"TO '{path}' (FORMAT parquet, COMPRESSION zstd)"
        )
        paths.append(path)
    csv = out_dir / "leads.csv"
    con.execute(
        f"""COPY (SELECT priority_rank, tier, legal_name, ja_kodas, main_category, n_places,
                         place_names, reviews_total, rating_weighted, taxes_paid, peer_median_taxes,
                         insured_avg, revenue, revenue_fy, score, gap_taxes, gap_payroll,
                         gap_revenue, sig_near_zero_declared, sig_staffing_floor, sig_vat_gap,
                         hold_reason, link_methods, validation_statuses
                  FROM mart.lead WHERE run_month = '{run_month}' ORDER BY priority_rank)
            TO '{csv}' (HEADER)"""
    )
    log.info("example.exported", dir=str(out_dir), tables=len(paths))
    return [*paths, csv]


def load_example(con: duckdb.DuckDBPyConnection, example_dir: Path) -> None:
    """Offline demo: build the warehouse tables the app reads from committed Parquet files."""
    for month_dir in sorted(p for p in example_dir.iterdir() if p.is_dir()):
        for name, (table, _) in EXAMPLE_TABLES.items():
            path = month_dir / f"{name}.parquet"
            if path.exists():
                con.execute(
                    f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_parquet('{path}')"
                )
    log.info("example.loaded", dir=str(example_dir))
