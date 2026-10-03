"""Smoke test: build the offline demo warehouse from the committed example and render every page."""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from shadowleads.db import run_sql_file, session
from shadowleads.export import load_example

EXAMPLES = Path(__file__).parent.parent / "examples"
PAGES = ["page_leads", "page_coverage", "page_history", "page_quality", "page_sql"]


@pytest.fixture(scope="module")
def demo_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not any(EXAMPLES.glob("*/lead.parquet")):
        pytest.skip("no committed example")
    data = tmp_path_factory.mktemp("demo")
    with session(data / "warehouse.duckdb") as con:
        load_example(con, EXAMPLES)
        run_sql_file(con, "mart_views.sql")
    return data


def _render(page: str) -> None:
    import sys

    sys.path.insert(0, "app")
    import streamlit_app

    getattr(streamlit_app, page)()


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(page: str, demo_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHADOWLEADS_DATA_DIR", str(demo_dir))
    monkeypatch.setenv("SHADOWLEADS_STATE_DIR", str(demo_dir / "state"))
    at = AppTest.from_function(_render, kwargs={"page": page}, default_timeout=120).run()
    if page == "page_sql":
        at.button[0].click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.dataframe) >= 1


def test_lead_history_explains_changes(demo_dir: Path) -> None:
    """A second snapshot with changed inputs shows up in mart.lead_history with the right reason."""
    import shutil

    import duckdb

    db = demo_dir / "history.duckdb"
    shutil.copy(demo_dir / "warehouse.duckdb", db)
    con = duckdb.connect(str(db))
    month = con.execute("SELECT max(run_month) FROM mart.lead").fetchone()[0]  # type: ignore[index]
    a, b = con.execute(
        "SELECT ja_kodas FROM mart.lead WHERE run_month = ? ORDER BY priority_rank LIMIT 2", [month]
    ).fetchall()
    con.execute(
        "CREATE TEMP TABLE nxt AS SELECT * REPLACE ('2099-01' AS run_month) FROM mart.lead WHERE run_month = ?",
        [month],
    )
    con.execute(
        "UPDATE nxt SET taxes_paid = taxes_paid + 50000, tier = 'C_not_flagged' WHERE ja_kodas = ?",
        [a[0]],
    )
    con.execute("UPDATE nxt SET reviews_total = reviews_total + 100 WHERE ja_kodas = ?", [b[0]])
    con.execute("INSERT INTO mart.lead SELECT * FROM nxt")
    rows = dict(
        con.execute(
            "SELECT ja_kodas, change_reason FROM mart.lead_history WHERE run_month = '2099-01' AND ja_kodas IN (?, ?)",
            [a[0], b[0]],
        ).fetchall()
    )
    assert rows[a[0]] == "declared figures changed"
    assert rows[b[0]] == "visible activity changed"
    changed = con.execute(
        "SELECT change_type FROM mart.lead_history WHERE run_month = '2099-01' AND ja_kodas = ?",
        [a[0]],
    ).fetchone()
    assert changed == ("tier_changed",)
