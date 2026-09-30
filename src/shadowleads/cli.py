"""Command-line entry point: `shadowleads <stage>`. Each stage is idempotent per run month."""

from __future__ import annotations

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
def fetch_official(run_month: RunMonth | None = None, *, skip_register: bool = False) -> None:
    """Download JAR, Sodra and VMI bulk data and stage it."""
    from shadowleads.sources import official

    s = get_settings()
    month = run_month or current_run_month()
    this_year = date.today().year
    with session(s.db_path) as con, PoliteClient(s.user_agent, min_interval=1.5) as client:
        official.fetch_jar(con, client, s.raw_dir, month)
        official.fetch_sodra(
            con, client, s.raw_dir, month, years=[this_year - 2, this_year - 1, this_year]
        )
        official.fetch_vmi_taxes(con, client, s.raw_dir, month, since_year=this_year - 2)
        if not skip_register:
            official.fetch_vmi_register(con, client, s.raw_dir, month)


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
def sql(query: str, *, db: Path | None = None) -> None:
    """Run an ad-hoc read-only SQL query against the warehouse and print the result."""
    s = get_settings()
    with session(db or s.db_path, read_only=True) as con:
        print(con.sql(query))


def main() -> None:
    app.meta()


if __name__ == "__main__":
    main()
