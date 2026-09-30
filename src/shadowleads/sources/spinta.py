"""Paginated bulk export from the Lithuanian open-data API (get.data.gov.lt, "Spinta").

Query syntax: `?select(a,b.c,_page)&field>=x&limit(n)`; follow `_page.next` with `&page("token")`.
`_page` must be in `select()` or no cursor is returned. Pages land as gzipped JSONL.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from urllib.parse import quote

from shadowleads.http import PoliteClient
from shadowleads.log import get_logger

log = get_logger(__name__)

BASE = "https://get.data.gov.lt/datasets/gov"
_SAFE = '()=&,.-_"*:<>|/'


def _flatten(row: dict[str, object], prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in row.items():
        if key.startswith("_") and key not in {"_id"}:
            continue
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, f"{name}__"))
        else:
            out[name] = value
    return out


def export_model(
    client: PoliteClient,
    model: str,
    fields: list[str],
    dest: Path,
    *,
    filters: str = "",
    page_size: int = 20_000,
) -> int:
    """Download every row of `model` (e.g. 'vmi/ja_mokesciai/Moketojas') into `dest` (.jsonl.gz).

    Dotted fields follow references (`mm_kodas.ja_kodas`) and are flattened to `mm_kodas__ja_kodas`.
    Returns the number of rows written.
    """
    select = "select(" + ",".join([*fields, "_page"]) + ")"
    # Stable output schema: a null reference comes back as `{"ref": null}`, not `ref.field`.
    keys = [f.replace(".", "__") for f in fields]
    query = "&".join(p for p in (select, filters, f"limit({page_size})") if p)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    rows = 0
    token: str | None = None
    with gzip.open(tmp, "wt", encoding="utf-8") as out:
        while True:
            q = query + (f'&page("{token}")' if token else "")
            url = f"{BASE}/{model}?{quote(q, safe=_SAFE)}"
            payload = client.get(url, expect="json").json()
            data = payload.get("_data", [])
            for row in data:
                flat = _flatten(row)
                out.write(json.dumps({k: flat.get(k) for k in keys}, ensure_ascii=False) + "\n")
            rows += len(data)
            token = (payload.get("_page") or {}).get("next")
            log.info("spinta.page", model=model, rows=rows)
            if not data or not token:
                break
    tmp.replace(dest)
    return rows


def query_model(client: PoliteClient, model: str, query: str) -> list[dict[str, object]]:
    """Small filtered query (e.g. one entity), returns flattened rows."""
    url = f"{BASE}/{model}?{quote(query, safe=_SAFE)}"
    payload = client.get(url, expect="json").json()
    return [_flatten(r) for r in payload.get("_data", [])]
