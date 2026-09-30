"""Declared-activity sources: JAR (Registrų centras), Sodra, VMI. Bulk downloads -> raw -> stg.

All raw artefacts are immutable (`data/raw/<source>/<date>/...`) and registered in
`meta.source_fetch`, so every staged row can be traced to a file + sha256.
"""

from __future__ import annotations

import gzip
import re
import zipfile
from datetime import date
from pathlib import Path

import duckdb

from shadowleads.db import record_fetch
from shadowleads.http import PoliteClient
from shadowleads.log import get_logger
from shadowleads.sources.spinta import export_model

log = get_logger(__name__)

JAR_URL = "https://www.registrucentras.lt/aduomenys/?byla=JAR_IREGISTRUOTI.csv"
SODRA_URL = "https://atvira.sodra.lt/imones/downloads/{year}/monthly-{year}.csv.zip"

VMI_TAXES_MODEL = "vmi/ja_mokesciai/Moketojas"
VMI_TAXES_FIELDS = ["_id", "mm_kodas.ja_kodas", "tipas", "metai", "menuo", "suma", "atnaujinta"]

VMI_REGISTER_MODEL = "vmi/mm_registras/MokesciuMoketojas"
VMI_REGISTER_FIELDS = [
    "_id", "ja_kodas", "pavadinimas", "isreg_data", "tipo_kodas", "pvm_kodas",
    "pvm_iregistruota", "pvm_isregistruota", "padalinio_nr", "padalinio_pvd",
    "padalinio_savivaldybe", "ekonomine_veikla.kodas", "veiklos_pradzia",
    "veiklos_pabaiga", "pagrindine", "vv_savivaldybe",
]  # fmt: skip

SODRA_COLUMNS = {
    "code": "VARCHAR", "jarCode": "BIGINT", "name": "VARCHAR", "municipality": "VARCHAR",
    "ecoActCode": "VARCHAR", "ecoActCodeStr": "VARCHAR", "ecoActName": "VARCHAR",
    "month": "INTEGER", "avgWage": "DOUBLE", "numInsured": "INTEGER", "avgWage2": "DOUBLE",
    "numInsured2": "INTEGER", "tax": "DOUBLE",
}  # fmt: skip


def _count_lines(path: Path) -> int:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return sum(1 for _ in f)


def _sodra_col(header_cell: str) -> str:
    """'Apdraustųjų skaičius (numInsured)' -> 'numInsured'."""
    m = re.search(r"\(([A-Za-z0-9]+)\)\s*$", header_cell)
    if not m:
        raise ValueError(f"unexpected Sodra header cell: {header_cell!r}")
    return m.group(1)


def _raw_path(raw_dir: Path, source: str, filename: str) -> Path:
    return raw_dir / source / date.today().isoformat() / filename


def _download_once(client: PoliteClient, url: str, dest: Path, expect: str) -> Path:
    """Raw files are immutable per day: re-running a stage reuses today's download."""
    return dest if dest.exists() else client.download(url, dest, expect=expect)


def fetch_jar(
    con: duckdb.DuckDBPyConnection, client: PoliteClient, raw_dir: Path, run_month: str
) -> None:
    dest = _download_once(client, JAR_URL, _raw_path(raw_dir, "jar", "JAR_IREGISTRUOTI.csv"), "csv")
    fid = record_fetch(con, source="jar", url=JAR_URL, path=dest, run_month=run_month)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE stg.jar_entity AS
        SELECT
            TRY_CAST(ja_kodas AS BIGINT)        AS ja_kodas,
            ja_pavadinimas                      AS legal_name,
            adresas                             AS registered_address,
            TRY_CAST(ja_reg_data AS DATE)       AS registered_on,
            TRY_CAST(form_kodas AS INTEGER)     AS legal_form_code,
            form_pavadinimas                    AS legal_form,
            TRY_CAST(stat_kodas AS INTEGER)     AS status_code,
            stat_pavadinimas                    AS status,
            TRY_CAST(stat_data_nuo AS DATE)     AS status_since,
            '{fid}'                             AS fetch_id
        FROM read_csv('{dest}', delim='|', quote='"', escape='"', header=true,
                      all_varchar=true, strict_mode=false)
        WHERE TRY_CAST(ja_kodas AS BIGINT) IS NOT NULL
        """
    )
    n = con.execute("SELECT count(*) FROM stg.jar_entity").fetchone()
    con.execute(
        "UPDATE meta.source_fetch SET row_count = ? WHERE fetch_id = ?", [n[0] if n else 0, fid]
    )
    log.info("jar.loaded", rows=n)


def fetch_sodra(
    con: duckdb.DuckDBPyConnection,
    client: PoliteClient,
    raw_dir: Path,
    run_month: str,
    years: list[int],
) -> None:
    csvs: list[tuple[Path, str]] = []
    for year in years:
        url = SODRA_URL.format(year=year)
        zpath = _download_once(
            client, url, _raw_path(raw_dir, "sodra", f"monthly-{year}.csv.zip"), "zip"
        )
        fid = record_fetch(con, source=f"sodra_{year}", url=url, path=zpath, run_month=run_month)
        with zipfile.ZipFile(zpath) as zf:
            member = zf.namelist()[0]
            csv_path = zpath.with_name(member)
            with zf.open(member) as src, csv_path.open("wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
        csvs.append((csv_path, fid))

    # The schema drifts between yearly files (2026 added `ecoActCodeStr`), so columns are taken
    # from each file's own header ("Pavadinimas (name)" -> name) and missing ones become NULL.
    selects = []
    for p, fid in csvs:
        header = p.open(encoding="utf-8-sig").readline()
        names = [_sodra_col(h) for h in header.strip().split(";")]
        cols = "{" + ", ".join(f"'{n}': '{SODRA_COLUMNS.get(n, 'VARCHAR')}'" for n in names) + "}"
        projection = ", ".join(n if n in names else f"NULL AS {n}" for n in SODRA_COLUMNS)
        selects.append(
            f"""SELECT {projection}, '{fid}' AS fetch_id
                FROM read_csv('{p}', delim=';', quote='"', escape='"', header=true,
                              columns={cols}, strict_mode=false)"""
        )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE stg.sodra_monthly AS
        SELECT jarCode AS ja_kodas, code AS sodra_code, name, municipality,
               coalesce(ecoActCodeStr, CASE WHEN length(ecoActCode) = 6 THEN
                   substr(ecoActCode, 1, 2) || '.' || substr(ecoActCode, 3, 2) || '.'
                   || substr(ecoActCode, 5, 2) END) AS evrk,
               ecoActName AS evrk_name,
               make_date(month // 100, month % 100, 1) AS month,
               avgWage AS avg_wage, numInsured AS num_insured,
               numInsured2 AS num_insured_other, tax AS contributions, fetch_id
        FROM ({" UNION ALL ".join(selects)})
        WHERE jarCode IS NOT NULL
        """
    )
    for p, _ in csvs:  # extracted CSVs are ~0.5 GB; the zip stays as the immutable raw artefact
        p.unlink(missing_ok=True)
    n = con.execute("SELECT count(*), max(month) FROM stg.sodra_monthly").fetchone()
    log.info("sodra.loaded", rows=n[0] if n else 0, latest_month=str(n[1]) if n else None)


def fetch_vmi_taxes(
    con: duckdb.DuckDBPyConnection,
    client: PoliteClient,
    raw_dir: Path,
    run_month: str,
    since_year: int,
) -> None:
    dest = _raw_path(raw_dir, "vmi_taxes", f"ja_mokesciai_since_{since_year}.jsonl.gz")
    rows = (
        _count_lines(dest)
        if dest.exists()
        else export_model(
            client, VMI_TAXES_MODEL, VMI_TAXES_FIELDS, dest, filters=f"metai>={since_year}"
        )
    )
    fid = record_fetch(
        con,
        source="vmi_taxes",
        url=f"get.data.gov.lt/{VMI_TAXES_MODEL}?metai>={since_year}",
        path=dest,
        run_month=run_month,
        row_count=rows,
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE stg.vmi_taxes AS
        SELECT CAST(mm_kodas__ja_kodas AS BIGINT) AS ja_kodas, tipas AS legal_form,
               CAST(metai AS INTEGER) AS year, CAST(menuo AS INTEGER) AS through_month,
               CAST(suma AS DOUBLE) AS taxes_paid, CAST(atnaujinta AS DATE) AS updated_on,
               '{fid}' AS fetch_id
        FROM read_json('{dest}', format='newline_delimited', union_by_name=true)
        WHERE mm_kodas__ja_kodas IS NOT NULL
        """
    )
    log.info("vmi_taxes.loaded", rows=rows)


def fetch_vmi_register(
    con: duckdb.DuckDBPyConnection, client: PoliteClient, raw_dir: Path, run_month: str
) -> None:
    dest = _raw_path(raw_dir, "vmi_register", "mm_registras.jsonl.gz")
    rows = (
        _count_lines(dest)
        if dest.exists()
        else export_model(client, VMI_REGISTER_MODEL, VMI_REGISTER_FIELDS, dest)
    )
    fid = record_fetch(
        con,
        source="vmi_register",
        url=f"get.data.gov.lt/{VMI_REGISTER_MODEL}",
        path=dest,
        run_month=run_month,
        row_count=rows,
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE stg.vmi_register AS
        SELECT CAST(ja_kodas AS BIGINT) AS ja_kodas, pavadinimas AS name,
               TRY_CAST(isreg_data AS DATE) AS deregistered_on,
               TRY_CAST(tipo_kodas AS INTEGER) AS taxpayer_type,
               pvm_kodas AS vat_code,
               TRY_CAST(pvm_iregistruota AS DATE) AS vat_registered_on,
               TRY_CAST(pvm_isregistruota AS DATE) AS vat_deregistered_on,
               TRY_CAST(padalinio_nr AS INTEGER) AS branch_no,
               padalinio_pvd AS branch_name,
               TRY_CAST(padalinio_savivaldybe AS INTEGER) AS branch_municipality,
               ekonomine_veikla__kodas AS activity_code,
               TRY_CAST(veiklos_pradzia AS DATE) AS activity_from,
               TRY_CAST(veiklos_pabaiga AS DATE) AS activity_to,
               TRY_CAST(pagrindine AS INTEGER) = 1 AS is_main_activity,
               TRY_CAST(vv_savivaldybe AS INTEGER) AS seat_municipality,
               '{fid}' AS fetch_id
        FROM read_json('{dest}', format='newline_delimited', union_by_name=true,
                       columns={{'ja_kodas': 'VARCHAR', 'pavadinimas': 'VARCHAR',
                                 'isreg_data': 'VARCHAR', 'tipo_kodas': 'VARCHAR',
                                 'pvm_kodas': 'VARCHAR', 'pvm_iregistruota': 'VARCHAR',
                                 'pvm_isregistruota': 'VARCHAR', 'padalinio_nr': 'VARCHAR',
                                 'padalinio_pvd': 'VARCHAR', 'padalinio_savivaldybe': 'VARCHAR',
                                 'ekonomine_veikla__kodas': 'VARCHAR', 'veiklos_pradzia': 'VARCHAR',
                                 'veiklos_pabaiga': 'VARCHAR', 'pagrindine': 'VARCHAR',
                                 'vv_savivaldybe': 'VARCHAR'}})
        WHERE ja_kodas IS NOT NULL
        """
    )
    log.info("vmi_register.loaded", rows=rows)


RC_PL_MODEL = "rc/jar/pelno_ataskaitos/PelnoAtaskaita"
RC_PL_FIELDS = [
    "_id", "juridinis_asmuo.ja_kodas", "reiksme", "laikotarpis_nuo", "laikotarpis_iki",
    "reg_date", "template_id",
]  # fmt: skip


def fetch_revenue(
    con: duckdb.DuckDBPyConnection,
    client: PoliteClient,
    raw_dir: Path,
    run_month: str,
    since_fy: int,
) -> None:
    """Sales revenue ("PARDAVIMO PAJAMOS") from filed profit & loss statements, FY >= since_fy.

    The open export lags (in 2026-09 it held FY2024 for ~230k companies but FY2025 for only ~7k),
    so every row keeps its fiscal year and the latest available year is used and labelled.
    Rows come in exact duplicate pairs in the source; they are de-duplicated here.
    """
    dest = _raw_path(raw_dir, "rc_revenue", f"pardavimo_pajamos_fy{since_fy}plus.jsonl.gz")
    filters = f'line_name="PARDAVIMO PAJAMOS"&laikotarpis_iki>="{since_fy}-01-01"'
    rows = (
        _count_lines(dest)
        if dest.exists()
        else export_model(client, RC_PL_MODEL, RC_PL_FIELDS, dest, filters=filters)
    )
    fid = record_fetch(
        con,
        source="rc_revenue",
        url=f"get.data.gov.lt/{RC_PL_MODEL}?{filters}",
        path=dest,
        run_month=run_month,
        row_count=rows,
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE stg.rc_revenue AS
        SELECT DISTINCT
            CAST(juridinis_asmuo__ja_kodas AS BIGINT) AS ja_kodas,
            TRY_CAST(laikotarpis_nuo AS DATE) AS period_from,
            TRY_CAST(laikotarpis_iki AS DATE) AS period_to,
            year(TRY_CAST(laikotarpis_iki AS DATE)) AS fiscal_year,
            TRY_CAST(reiksme AS DOUBLE) AS revenue,
            TRY_CAST(reg_date AS DATE) AS filed_on,
            '{fid}' AS fetch_id
        FROM read_json('{dest}', format='newline_delimited', columns={{
            '_id':'VARCHAR','juridinis_asmuo__ja_kodas':'VARCHAR','reiksme':'VARCHAR',
            'laikotarpis_nuo':'VARCHAR','laikotarpis_iki':'VARCHAR','reg_date':'VARCHAR',
            'template_id':'VARCHAR'}})
        WHERE juridinis_asmuo__ja_kodas IS NOT NULL
        """
    )
    log.info("rc_revenue.loaded", rows=rows)
