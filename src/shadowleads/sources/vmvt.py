"""VMVT (State Food and Veterinary Service) register of food business premises - nightlife linking.

The open-data copy on data.gov.lt has null codes/addresses, so the public search form at
vmvt.lt/opendata/mtsr is queried once per relevant activity with address filter "Vilnius".
Each row gives company code + trade name + premises address: premises-level evidence.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import duckdb
from selectolax.parser import HTMLParser

from shadowleads.db import record_fetch
from shadowleads.http import PoliteClient
from shadowleads.log import get_logger

log = get_logger(__name__)

URL = "https://vmvt.lt/opendata/mtsr/index.php"
# VMVT activity ids (form option values) relevant to bars / pubs / clubs.
ACTIVITIES = {
    "20800000000000000002": "56.30.0.G Gėrimų pardavimo vartoti vietoje veikla",
    "20800000000000000288": "56.10.0.KR Kavinių, užkandinių, restoranų veikla",
    "20800000000000000131": "56.10.0.K Kavinių, užkandinių veikla",
    "20800000000000000109": "56.10.0.R Restoranų veikla",
    "20800000000000000107": "56.0 Maitinimo ir gėrimų teikimo veikla",
}
COLUMNS = [
    "row_no", "ja_kodas", "operator_name", "trade_name", "main_activity", "other_activities",
    "assortment", "address", "issuer", "issued_on", "certificate_no", "risk_group",
]  # fmt: skip


def parse_results(html: str) -> list[dict[str, str]]:
    tables = HTMLParser(html).css("table")
    if len(tables) < 2:
        return []
    rows = []
    for tr in tables[1].css("tr")[1:]:
        cells = [td.text(strip=True) for td in tr.css("td")]
        if len(cells) == len(COLUMNS):
            rows.append(dict(zip(COLUMNS, cells, strict=True)))
    return rows


def fetch_vmvt(
    con: duckdb.DuckDBPyConnection, client: PoliteClient, raw_dir: Path, run_month: str
) -> int:
    out = raw_dir / "vmvt" / date.today().isoformat() / "premises.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with out.open("w", encoding="utf-8") as f:
        for evr_id, label in ACTIVITIES.items():
            form = {
                "ORG_ID": "", "EVR_ID": evr_id, "SV_KODAS": "", "SV_PAVADINIMAS": "",
                "MT_PAVADINIMAS": "", "MT_ADRESAS": "Vilnius", "Submit": "Ieškoti",
            }  # fmt: skip
            html = client.post(URL, data=form, expect="html").text
            (out.parent / f"{evr_id}.html").write_text(html, encoding="utf-8")
            rows = parse_results(html)
            for r in rows:
                f.write(json.dumps({**r, "query_activity": label}, ensure_ascii=False) + "\n")
            total += len(rows)
            log.info("vmvt.activity", activity=label[:40], rows=len(rows))
    fid = record_fetch(con, source="vmvt", url=URL, path=out, run_month=run_month, row_count=total)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE stg.vmvt_premises AS
        SELECT DISTINCT
            TRY_CAST(ja_kodas AS BIGINT) AS ja_kodas, operator_name, trade_name, main_activity,
            other_activities, address, TRY_CAST(issued_on AS DATE) AS issued_on, certificate_no,
            '{fid}' AS fetch_id
        FROM read_json('{out}', format='newline_delimited', columns={{
            'ja_kodas':'VARCHAR','operator_name':'VARCHAR','trade_name':'VARCHAR',
            'main_activity':'VARCHAR','other_activities':'VARCHAR','address':'VARCHAR',
            'issued_on':'VARCHAR','certificate_no':'VARCHAR'}})
        """
    )
    return total
