"""Smoke test: build the offline demo warehouse from the committed example and render every page."""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from shadowleads.db import run_sql_file, session
from shadowleads.export import load_example

EXAMPLES = Path(__file__).parent.parent / "examples"
PAGES = ["page_leads", "page_coverage", "page_quality", "page_sql"]


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
    at = AppTest.from_function(_render, kwargs={"page": page}, default_timeout=120).run()
    if page == "page_sql":
        at.button[0].click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.dataframe) >= 1
