"""Command-line entry point: `shadowleads <stage>`. Each stage is idempotent per run month."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Annotated

import cyclopts

from shadowleads.db import session
from shadowleads.http import PoliteClient
from shadowleads.log import configure_logging, get_logger
from shadowleads.settings import VILNIUS_BBOX, current_run_month, get_settings

app = cyclopts.App(name="shadowleads", help="Vilnius shadow-economy lead list pipeline.")
log = get_logger("shadowleads.cli")

RunMonth = Annotated[str, cyclopts.Parameter(help="Run month YYYY-MM (default: current month).")]


@app.meta.default
def _meta(
    *tokens: Annotated[str, cyclopts.Parameter(show=False, allow_leading_hyphen=True)],
) -> None:
    configure_logging()
    app(tokens)


@app.command
def fetch_official(
    run_month: RunMonth | None = None, *, skip_register: bool = False, only_revenue: bool = False
) -> None:
    """Download JAR, Sodra and VMI bulk data and stage it."""
    from shadowleads.sources import official

    s = get_settings()
    month = run_month or current_run_month()
    this_year = date.today().year
    with session(s.db_path) as con, PoliteClient(s.user_agent, min_interval=1.5) as client:
        if only_revenue:
            official.fetch_revenue(con, client, s.raw_dir, month, since_fy=this_year - 3)
            return
        official.fetch_jar(con, client, s.raw_dir, month)
        official.fetch_sodra(
            con, client, s.raw_dir, month, years=[this_year - 2, this_year - 1, this_year]
        )
        official.fetch_vmi_taxes(con, client, s.raw_dir, month, since_year=this_year - 2)
        if not skip_register:
            official.fetch_vmi_register(con, client, s.raw_dir, month)
        official.fetch_revenue(con, client, s.raw_dir, month, since_fy=this_year - 3)


@app.command
def fetch_google(
    run_month: RunMonth | None = None,
    *,
    bbox: Annotated[
        tuple[float, float, float, float] | None,
        cyclopts.Parameter(
            help="south west north east; default = Vilnius. Use a small box for dev."
        ),
    ] = None,
) -> None:
    """Sweep Google Places (Nearby Search, quadtree) and stage a place snapshot."""
    from shadowleads.sources.google_places import PlacesCollector, all_primary_types, load_snapshot

    s = get_settings()
    if not s.has_google or s.google_maps_api_key is None:
        raise SystemExit("GOOGLE_MAPS_API_KEY is not set")
    month = run_month or current_run_month()
    cache_dir = s.raw_dir / "google" / month / "responses"
    with session(s.db_path) as con:
        collector = PlacesCollector(
            con, s.google_maps_api_key.get_secret_value(), cache_dir, month, s.google_budget
        )
        manifest = collector.sweep(bbox or VILNIUS_BBOX, all_primary_types())
        load_snapshot(con, cache_dir, manifest, month)
        log.info("google.calls_this_month", calls=collector.calls_this_billing_month())


@app.command
def link(run_month: RunMonth | None = None) -> None:
    """Build entity + place models and link Google places to legal entities (primary rules)."""
    from shadowleads.db import run_sql_file
    from shadowleads.linking.matcher import run_primary_linking

    s = get_settings()
    month = run_month or current_run_month()
    with session(s.db_path) as con:
        run_sql_file(con, "core_entity.sql")
        run_sql_file(con, "core_place.sql", run_month=month)
        run_primary_linking(con, month)


@app.command
def fetch_websites(run_month: RunMonth | None = None, *, workers: int = 12) -> None:
    """Scan businesses' own websites for self-declared company / VAT codes."""
    from shadowleads.db import record_fetch
    from shadowleads.sources.websites import scan_sites

    s = get_settings()
    month = run_month or current_run_month()
    out = s.raw_dir / "websites" / month / "codes.jsonl"
    with session(s.db_path) as con:
        places = con.execute(
            "SELECT place_id, website FROM core.place_snapshot "
            "WHERE run_month = ? AND in_scope AND website IS NOT NULL",
            [month],
        ).fetchall()
    rows = scan_sites(places, s.user_agent, s.raw_dir / "websites" / month / "pages", out, workers)
    with session(s.db_path) as con:
        record_fetch(
            con,
            source="websites",
            url="(business websites)",
            path=out,
            run_month=month,
            row_count=rows,
        )
        con.execute(
            f"""
            CREATE TABLE IF NOT EXISTS stg.website_code (
                run_month VARCHAR, place_id VARCHAR, site VARCHAR, page_url VARCHAR,
                code_type VARCHAR, code VARCHAR, context VARCHAR, fetched_at TIMESTAMP);
            DELETE FROM stg.website_code WHERE run_month = '{month}';
            INSERT INTO stg.website_code
            SELECT '{month}', place_id, coalesce(site, 'https://' || host), page_url, code_type, code,
                   context, CAST(fetched_at AS TIMESTAMPTZ)::TIMESTAMP
            FROM read_json('{out}', format='newline_delimited', columns={{
                'place_id':'VARCHAR','site':'VARCHAR','host':'VARCHAR','page_url':'VARCHAR',
                'code_type':'VARCHAR','code':'VARCHAR','context':'VARCHAR','fetched_at':'VARCHAR'}})
            """
        )
    log.info("websites.done", codes=rows, places=len(places))


@app.command
def fetch_vmvt(run_month: RunMonth | None = None) -> None:
    """Fetch VMVT food-business premises in Vilnius (nightlife linking evidence)."""
    from shadowleads.sources.vmvt import fetch_vmvt

    s = get_settings()
    month = run_month or current_run_month()
    with session(s.db_path) as con, PoliteClient(s.user_agent, min_interval=2.0) as client:
        n = fetch_vmvt(con, client, s.raw_dir, month)
    log.info("vmvt.done", rows=n)


@app.command
def fetch_serp(
    run_month: RunMonth | None = None,
    *,
    fallback_n: int = 200,
    validation_n: int = 100,
    min_reviews: int = 30,
) -> None:
    """Oxylabs Google-search lookups of company codes: busiest unresolved places + a random
    validation sample of primary links."""
    from shadowleads.linking.fallback import serp_targets
    from shadowleads.sources.oxylabs import SerpClient, run_serp_lookups

    s = get_settings()
    if not s.has_oxylabs or s.oxylabs_password is None or s.oxylabs_username is None:
        raise SystemExit("OXYLABS_USERNAME / OXYLABS_PASSWORD are not set")
    month = run_month or current_run_month()
    with session(s.db_path) as con:
        targets = serp_targets(
            con, month, fallback_n=fallback_n, validation_n=validation_n, min_reviews=min_reviews
        )
        client = SerpClient(
            con,
            username=s.oxylabs_username,
            password=s.oxylabs_password.get_secret_value(),
            api_url=s.oxylabs_api_url,
            geo_location=s.oxylabs_geo_location,
            cache_dir=s.raw_dir / "serp" / month,
            run_month=month,
            budget=s.oxylabs_budget,
        )
        rows = run_serp_lookups(client, targets)
        con.execute(
            """CREATE TABLE IF NOT EXISTS stg.serp_code (
                run_month VARCHAR, place_id VARCHAR, purpose VARCHAR, query VARCHAR,
                code VARCHAR, hits INTEGER, hits_named INTEGER)"""
        )
        con.execute("DELETE FROM stg.serp_code WHERE run_month = ?", [month])
        cols = ["place_id", "purpose", "query", "code", "hits", "hits_named"]
        if rows:
            con.executemany(
                "INSERT INTO stg.serp_code VALUES (?, ?, ?, ?, ?, ?, ?)",
                [[month, *(r[c] for c in cols)] for r in rows],
            )
        log.info("serp.done", targets=len(targets), rows=len(rows), calls=client.calls_this_run())


@app.command
def fetch_trademarks(run_month: RunMonth | None = None, *, limit: int = 800) -> None:
    """Trademark owners (LINTA) for visibly busy places - brand-level evidence."""
    from shadowleads.linking.brand_evidence import busy_targets, collect_trademarks
    from shadowleads.sources.trademarks import TrademarkClient

    s = get_settings()
    month = run_month or current_run_month()
    with session(s.db_path) as con, PoliteClient(s.user_agent, min_interval=1.5) as client:
        targets = busy_targets(con, month, quantile=0.75, only_unresolved=False, limit=limit)
        n = collect_trademarks(con, TrademarkClient(client, s.raw_dir / "linta"), month, targets)
    log.info("trademarks.done", targets=len(targets), owners=n)


@app.command
def fetch_job_ads(
    run_month: RunMonth | None = None, *, limit: int = 150, budget: int = 500
) -> None:
    """Employers named in job ads for busy unresolved brands (Oxylabs Google search)."""
    from shadowleads.linking.brand_evidence import brand_of, busy_targets, store_job_ads
    from shadowleads.sources.oxylabs import SerpClient, job_ad_query, run_serp_lookups

    s = get_settings()
    if not s.has_oxylabs or s.oxylabs_password is None or s.oxylabs_username is None:
        raise SystemExit("OXYLABS_USERNAME / OXYLABS_PASSWORD are not set")
    month = run_month or current_run_month()
    with session(s.db_path) as con:
        targets = busy_targets(con, month, quantile=0.5, only_unresolved=True, limit=limit)
        brands = {pid: brand_of(name) for pid, _, name, *_ in targets}
        client = SerpClient(
            con, username=s.oxylabs_username, password=s.oxylabs_password.get_secret_value(),
            api_url=s.oxylabs_api_url, geo_location=s.oxylabs_geo_location,
            cache_dir=s.raw_dir / "serp" / month, run_month=month, budget=budget,
        )  # fmt: skip
        fetched = run_serp_lookups(
            client,
            [(pid, "job_ads", brands[pid], None, None) for pid in brands],
            query_fn=lambda brand, *_: job_ad_query(brand),
            fetch_only=True,
        )
        n = store_job_ads(con, month, fetched, brands)
        log.info("job_ads.done", targets=len(targets), employers=n, calls=client.calls_this_run())


@app.command
def link_fallback(run_month: RunMonth | None = None) -> None:
    """Resolve ambiguous/unmatched places with independent code evidence; cross-check links."""
    from shadowleads.linking.fallback import run_fallback

    s = get_settings()
    with session(s.db_path) as con:
        run_fallback(con, run_month or current_run_month())


@app.command
def validate(run_month: RunMonth | None = None) -> None:
    """Link validation (V1-V4) and the entity-activity mart."""
    from shadowleads.db import run_sql_file

    s = get_settings()
    month = run_month or current_run_month()
    labels = Path("labels/match_audit.csv")
    with session(s.db_path) as con:
        if labels.exists():
            con.execute(
                "CREATE OR REPLACE TABLE core.link_audit_label AS "
                "SELECT place_id, CAST(ja_kodas AS BIGINT) AS ja_kodas, lower(trim(verdict)) AS verdict, "
                "auditor, TRY_CAST(audited_on AS DATE) AS audited_on, note "
                "FROM read_csv(?, header=true, all_varchar=true) "
                "WHERE lower(trim(verdict)) IN ('correct', 'wrong')",
                [str(labels)],
            )
        overrides = Path("labels/link_overrides.csv")
        if overrides.exists():
            # analyst decisions win over every automatic rule, every month (V4 feedback loop)
            con.execute(
                f"""
                CREATE OR REPLACE TABLE core.link_override AS
                SELECT place_id, CAST(ja_kodas AS BIGINT) AS ja_kodas, lower(action) AS action,
                       reason, analyst, TRY_CAST(decided_on AS DATE) AS decided_on
                FROM read_csv('{overrides}', header=true, all_varchar=true);
                UPDATE core.place_entity_link l
                SET status = 'linked', ja_kodas = o.ja_kodas, method = 'analyst_override',
                    confidence = 'HIGH', stage = 'override', reason = o.reason
                FROM core.link_override o
                WHERE l.run_month = '{month}' AND l.place_id = o.place_id AND o.action = 'set';
                UPDATE core.place_entity_link l
                SET status = 'ambiguous', ja_kodas = NULL, method = NULL, confidence = NULL,
                    stage = 'override', reason = 'rejected by analyst: ' || o.reason
                FROM core.link_override o
                WHERE l.run_month = '{month}' AND l.place_id = o.place_id AND o.action = 'reject';
                """
            )
        run_sql_file(con, "core_link_validation.sql", run_month=month)
        run_sql_file(con, "mart_entity_activity.sql", run_month=month)


@app.command
def audit_sample(run_month: RunMonth | None = None, *, per_stratum: int = 4) -> None:
    """Write a stratified random sample of links (category x method family) to
    labels/match_audit.csv for manual verification (V3). Fill `verdict` with correct|wrong."""
    s = get_settings()
    month = run_month or current_run_month()
    out = Path("labels/match_audit.csv")
    out.parent.mkdir(exist_ok=True)
    with session(s.db_path, read_only=True) as con:
        con.execute(
            f"""
            COPY (
                WITH l AS (
                    SELECT v.*, p.name AS place_name, p.formatted_address AS place_address,
                           p.maps_uri, p.website, e.legal_name, e.registered_address,
                           CASE WHEN v.method LIKE 'fallback%' THEN 'fallback'
                                WHEN v.method LIKE 'exact%' THEN 'exact_name'
                                WHEN v.method LIKE 'core%' OR v.method LIKE 'name_part%' THEN 'core_name'
                                ELSE v.method END AS method_family
                    FROM core.link_validation v
                    JOIN core.place_snapshot p USING (run_month, place_id)
                    JOIN core.entity e USING (ja_kodas)
                    WHERE v.run_month = '{month}'
                ),
                ranked AS (
                    SELECT *, row_number() OVER (
                        PARTITION BY category, method_family ORDER BY hash(place_id || '{month}')) AS rn
                    FROM l
                )
                SELECT place_id, ja_kodas, '' AS verdict, '' AS auditor, '' AS audited_on, '' AS note,
                       category, method_family, method, validation_status, place_name, place_address,
                       maps_uri, website, legal_name, registered_address,
                       'https://rekvizitai.vz.lt/paieska/?name=' || ja_kodas AS lookup_hint
                FROM ranked WHERE rn <= {per_stratum}
                ORDER BY category, method_family
            ) TO '{out}' (HEADER, DELIMITER ',')
            """
        )
    log.info("audit_sample.written", path=str(out))


@app.command
def score(run_month: RunMonth | None = None) -> None:
    """Score entities against peers and assign lead tiers (mart.lead + analyst views)."""
    from shadowleads.db import run_sql_file

    s = get_settings()
    month = run_month or current_run_month()
    with session(s.db_path) as con:
        run_sql_file(con, "mart_lead.sql", run_month=month)
        run_sql_file(con, "mart_views.sql")
        rows = con.execute(
            "SELECT tier, count(*) FROM mart.lead WHERE run_month = ? GROUP BY 1 ORDER BY 1",
            [month],
        ).fetchall()
    log.info("score.done", **{t: n for t, n in rows})


@app.command
def report(run_month: RunMonth | None = None) -> None:
    """Write output/MONTH/coverage.md (Milestone-1 checkpoint)."""
    from shadowleads.report import coverage_report

    s = get_settings()
    month = run_month or current_run_month()
    with session(s.db_path, read_only=True) as con:
        path = coverage_report(con, month, s.output_dir / month)
    log.info("report.written", path=str(path))


@app.command
def dq(run_month: RunMonth | None = None) -> None:
    """Run data-quality assertions; exits non-zero when an `error` check fails."""
    from shadowleads.db import run_sql_file

    s = get_settings()
    month = run_month or current_run_month()
    with session(s.db_path) as con:
        run_sql_file(con, "dq_checks.sql", run_month=month)
        failed = con.execute(
            "SELECT check_name, severity, observed, expected FROM meta.dq_result "
            "WHERE run_month = ? AND NOT passed ORDER BY severity",
            [month],
        ).fetchall()
    for name, severity, observed, expected in failed:
        log.warning(
            "dq.failed", check=name, severity=severity, observed=observed, expected=expected
        )
    if any(sev == "error" for _, sev, _, _ in failed):
        raise SystemExit("data-quality errors - lead export blocked")
    log.info("dq.done", failed_warnings=len(failed))


@app.command
def export(run_month: RunMonth | None = None) -> None:
    """Local analyst export with real names (output/MONTH/leads.csv + coverage.md)."""
    from shadowleads.export import export_run
    from shadowleads.report import coverage_report

    s = get_settings()
    month = run_month or current_run_month()
    with session(s.db_path, read_only=True) as con:
        errors = con.execute(
            "SELECT count(*) FROM meta.dq_result WHERE run_month = ? AND severity = 'error' AND NOT passed",
            [month],
        ).fetchone()
        if errors and errors[0]:
            raise SystemExit("data-quality errors - lead export blocked (see `shadowleads dq`)")
        path = export_run(con, month, s.output_dir / month)
        coverage_report(con, month, s.output_dir / month)
    log.info("export.done", path=str(path))


@app.command
def export_example(run_month: RunMonth | None = None, *, out: Path = Path("examples")) -> None:
    """Pseudonymised public example (committed to git) - see shadowleads.export."""
    from shadowleads.export import export_example as _export

    s = get_settings()
    month = run_month or current_run_month()
    key = s.pseudonym_key.get_secret_value().encode()
    with session(s.db_path) as con:
        _export(con, month, out / month, key)


@app.command
def demo(*, examples: Path = Path("examples")) -> None:
    """Offline demo: rebuild the warehouse from the committed pseudonymised example."""
    from shadowleads.db import run_sql_file
    from shadowleads.export import load_example

    s = get_settings()
    s.db_path.unlink(missing_ok=True)
    with session(s.db_path) as con:
        load_example(con, examples)
        run_sql_file(con, "mart_views.sql")
    log.info("demo.ready", db=str(s.db_path))


@app.command
def run(run_month: RunMonth | None = None, *, skip_official: bool = False) -> None:
    """Full monthly run: ingest -> link -> fallbacks -> validate -> score -> DQ -> export."""
    s = get_settings()
    month = run_month or current_run_month()
    log.info("run.start", run_month=month, google=s.has_google, oxylabs=s.has_oxylabs)
    if not skip_official:
        fetch_official(month)
    fetch_google(month)
    link(month)
    fetch_websites(month)
    fetch_vmvt(month)
    if s.has_oxylabs:
        fetch_serp(month)
    fetch_trademarks(month)
    if s.has_oxylabs:
        fetch_job_ads(month)
    link_fallback(month)
    validate(month)
    score(month)
    dq(month)
    export(month)
    log.info("run.done", run_month=month)


@app.command
def auto() -> None:
    """Container entry point: live run when a Google key is configured, otherwise offline demo."""
    s = get_settings()
    month = os.environ.get("SHADOWLEADS_RUN_MONTH") or current_run_month()
    if s.has_google:
        run(month)
    else:
        log.info("auto.no_google_key", action="serving the committed pseudonymised example")
        demo()


@app.command
def sql(query: str, *, db: Path | None = None) -> None:
    """Run an ad-hoc read-only SQL query against the warehouse and print the result."""
    s = get_settings()
    with session(db or s.db_path, read_only=True) as con:
        print(con.sql(query))


def main() -> None:
    app.meta()


if __name__ == "__main__":
    main()
