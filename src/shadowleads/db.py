"""DuckDB warehouse helpers: connection, schema bootstrap, SQL-file runner, fetch lineage."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import duckdb

SQL_DIR = Path(__file__).parent / "sql"

BOOTSTRAP_DDL = """
CREATE SCHEMA IF NOT EXISTS meta;
CREATE SCHEMA IF NOT EXISTS stg;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS mart;
CREATE SCHEMA IF NOT EXISTS ref;

CREATE TABLE IF NOT EXISTS meta.run (
    run_id      VARCHAR PRIMARY KEY,
    run_month   VARCHAR NOT NULL,
    stage       VARCHAR NOT NULL,
    started_at  TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    status      VARCHAR NOT NULL,
    git_sha     VARCHAR,
    details     JSON
);

-- One row per downloaded artefact: the lineage anchor every lead points back to.
CREATE TABLE IF NOT EXISTS meta.source_fetch (
    fetch_id    VARCHAR PRIMARY KEY,
    source      VARCHAR NOT NULL,
    url         VARCHAR NOT NULL,
    fetched_at  TIMESTAMP NOT NULL,
    path        VARCHAR NOT NULL,
    sha256      VARCHAR NOT NULL,
    bytes       BIGINT NOT NULL,
    row_count   BIGINT,
    run_month   VARCHAR NOT NULL
);

-- Every billable external API call (Google, Oxylabs). Used to enforce budgets.
CREATE TABLE IF NOT EXISTS meta.api_ledger (
    called_at    TIMESTAMP NOT NULL,
    run_month    VARCHAR NOT NULL,
    provider     VARCHAR NOT NULL,
    sku          VARCHAR NOT NULL,
    request_hash VARCHAR NOT NULL,
    http_status  INTEGER
);

CREATE TABLE IF NOT EXISTS meta.dq_result (
    run_month  VARCHAR NOT NULL,
    check_name VARCHAR NOT NULL,
    severity   VARCHAR NOT NULL,   -- error | warn
    passed     BOOLEAN NOT NULL,
    observed   VARCHAR,
    expected   VARCHAR,
    checked_at TIMESTAMP NOT NULL
);
"""


def connect(path: Path, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        con.execute(BOOTSTRAP_DDL)
    return con


@contextmanager
def session(path: Path, *, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    con = connect(path, read_only=read_only)
    try:
        yield con
    finally:
        con.close()


def run_sql_file(con: duckdb.DuckDBPyConnection, name: str, **params: str) -> None:
    """Execute a SQL file from `shadowleads/sql`. `{param}` placeholders are for trusted,
    internally generated values only (run_month etc.), never user input."""
    sql = (SQL_DIR / name).read_text(encoding="utf-8")
    if params:
        sql = sql.format(**params)
    con.execute(sql)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def record_fetch(
    con: duckdb.DuckDBPyConnection,
    *,
    source: str,
    url: str,
    path: Path,
    run_month: str,
    row_count: int | None = None,
) -> str:
    fetch_id = uuid.uuid4().hex[:16]
    con.execute(
        "INSERT INTO meta.source_fetch VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            fetch_id,
            source,
            url,
            datetime.now(UTC).replace(tzinfo=None),
            str(path),
            sha256_file(path),
            path.stat().st_size,
            row_count,
            run_month,
        ],
    )
    return fetch_id


def latest_fetch(con: duckdb.DuckDBPyConnection, source: str) -> tuple[str, Path] | None:
    row = con.execute(
        "SELECT fetch_id, path FROM meta.source_fetch WHERE source = ? "
        "ORDER BY fetched_at DESC LIMIT 1",
        [source],
    ).fetchone()
    return (row[0], Path(row[1])) if row else None
