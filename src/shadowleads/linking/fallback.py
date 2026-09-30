"""Fallback linking + independent cross-source validation (V2).

Independent evidence per place (none of it uses the name/address rules of the primary stage):
  listing_url  - the Google "website" is a company-directory page (rekvizitai.vz.lt/imone/<slug>)
  vmvt         - VMVT food-premises register: company code at the same premises address
  website      - company / VAT code self-declared on the business's own website
  serp         - company code quoted in Google result snippets (Oxylabs)

For primary-linked places the evidence either confirms the link, conflicts with it, or is absent.
For ambiguous / unmatched places a candidate needs evidence AND corroboration (activity fits the
category, address agrees, similar name, or it was already a primary candidate); a single surviving
entity - or one with strictly more support than any other - is linked with MEDIUM confidence.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

import duckdb
from rapidfuzz import fuzz

from shadowleads.linking.matcher import (
    Candidate,
    EntityIndex,
    LinkDecision,
    _candidate,
    write_decisions,
)
from shadowleads.linking.normalize import exact_key, name_key, parse_lt_address, parse_street
from shadowleads.log import get_logger

log = get_logger(__name__)

_REKVIZITAI_RE = re.compile(r"rekvizitai\.vz\.lt/(?:en/)?(?:imone|company)/([^/?#]+)")


@dataclass(slots=True)
class PlaceRow:
    place_id: str
    name: str
    category: str
    street: str | None
    number: str | None
    website: str | None
    status: str
    ja_kodas: int | None


def listing_slug_name(url: str | None) -> str | None:
    """'https://rekvizitai.vz.lt/imone/uab_roginta/' -> 'uab roginta'."""
    m = _REKVIZITAI_RE.search(url or "")
    return m.group(1).replace("_", " ").replace("-", " ") if m else None


def _load_evidence(
    con: duckdb.DuckDBPyConnection, run_month: str
) -> dict[str, dict[str, set[str]]]:
    """place_id -> source -> set of raw codes ('c:<9 digits>' company, 'v:<digits>' VAT)."""
    ev: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    tables = {
        r[0]
        for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='stg'"
        ).fetchall()
    }
    if "website_code" in tables:
        for pid, kind, code in con.execute(
            "SELECT place_id, code_type, code FROM stg.website_code WHERE run_month = ? AND code IS NOT NULL",
            [run_month],
        ).fetchall():
            ev[pid]["website"].add(("c:" if kind == "company" else "v:") + code)
    if "serp_code" in tables:
        for pid, code in con.execute(
            "SELECT place_id, code FROM stg.serp_code WHERE run_month = ? AND code IS NOT NULL",
            [run_month],
        ).fetchall():
            ev[pid]["serp"].add("c:" + code)
    return ev


def run_fallback(con: duckdb.DuckDBPyConnection, run_month: str) -> dict[str, int]:
    index = EntityIndex(con)
    vat_to_ja = dict(
        con.execute(
            "SELECT DISTINCT ltrim(vat_code, 'LT'), ja_kodas FROM stg.vmi_register WHERE vat_code IS NOT NULL"
        ).fetchall()
    )
    vmvt_by_addr: dict[str, list[tuple[int, str]]] = defaultdict(list)
    has_vmvt = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='stg' AND table_name='vmvt_premises'"
    ).fetchone()
    if has_vmvt and has_vmvt[0]:
        for ja, trade, address in con.execute(
            "SELECT ja_kodas, trade_name, address FROM stg.vmvt_premises WHERE ja_kodas IS NOT NULL"
        ).fetchall():
            a = parse_lt_address(address)
            if a and a.key:
                vmvt_by_addr[a.key].append((ja, trade or ""))

    evidence = _load_evidence(con, run_month)
    primary_cands: dict[str, set[int]] = defaultdict(set)
    for pid, ja in con.execute(
        "SELECT place_id, ja_kodas FROM core.match_candidate WHERE run_month = ? AND stage = 'primary'",
        [run_month],
    ).fetchall():
        primary_cands[pid].add(ja)

    rows = [
        PlaceRow(*r)
        for r in con.execute(
            """
            SELECT p.place_id, p.name, p.category, p.street, p.street_number, p.website,
                   l.status, l.ja_kodas
            FROM core.place_snapshot p
            JOIN core.place_entity_link l USING (run_month, place_id)
            WHERE p.run_month = ? AND p.in_scope AND l.stage = 'primary'
            """,
            [run_month],
        ).fetchall()
    ]

    agreement: list[tuple[str, str, str, str]] = []  # place_id, source, verdict, codes
    decisions: list[LinkDecision] = []
    stats: dict[str, int] = defaultdict(int)

    for p in rows:
        addr = parse_street(p.street, p.number)
        address_key = addr.key if addr else None
        support: dict[int, set[str]] = defaultdict(set)
        seen_codes: dict[str, set[str]] = defaultdict(set)

        # independent evidence -> entity ids
        for source, codes in evidence.get(p.place_id, {}).items():
            for raw in codes:
                seen_codes[source].add(raw)
                ja = int(raw[2:]) if raw.startswith("c:") else vat_to_ja.get(raw[2:])
                if ja is not None and ja in index.entities:
                    support[ja].add(source)
        if slug := listing_slug_name(p.website):
            seen_codes["listing_url"].add(slug)
            for ja, _ in index.by_exact.get(exact_key(slug), ()):
                support[ja].add("listing_url")
        if p.category == "nightlife" and address_key:
            for ja, trade in vmvt_by_addr.get(address_key, []):
                seen_codes["vmvt"].add(f"c:{ja}")
                if ja in index.entities:
                    same_name = fuzz.token_set_ratio(name_key(p.name), name_key(trade)) >= 80
                    if same_name or len(vmvt_by_addr[address_key]) == 1:
                        support[ja].add("vmvt")

        # V2: does the independent evidence agree with the primary link?
        if p.status == "linked" and p.ja_kodas is not None:
            for source in sorted(seen_codes):
                agrees = source in support.get(p.ja_kodas, set())
                verdict = "confirms" if agrees else "conflicts"
                agreement.append(
                    (p.place_id, source, verdict, ",".join(sorted(seen_codes[source])))
                )
                stats[f"v2:{source}:{verdict}"] += 1
            continue

        # fallback linking for ambiguous / unmatched places
        cands: list[Candidate] = []
        corroborated: dict[int, set[str]] = {}
        for ja, sources in support.items():
            c = _candidate(
                index,
                ja,
                "+".join(sorted(sources)),
                "",
                "fallback",
                p.category,
                address_key,
                p.name,
            )
            cands.append(c)
            in_primary = ja in primary_cands.get(p.place_id, set())
            if c.evrk_consistent or c.address_agrees or c.name_similarity >= 60 or in_primary:
                corroborated[ja] = sources | ({"primary_candidate"} if in_primary else set())
        if not corroborated:
            if cands:
                stats["fallback:uncorroborated"] += 1
            continue
        ranked = sorted(corroborated.items(), key=lambda kv: len(kv[1]), reverse=True)
        if len(ranked) == 1 or len(ranked[0][1]) > len(ranked[1][1]):
            ja, sources = ranked[0]
            method = "fallback:" + "+".join(sorted(sources))
            decisions.append(
                LinkDecision(
                    p.place_id, "linked", ja, method, "MEDIUM", "independent code evidence", cands
                )
            )
            stats[method] += 1
        else:
            stats["fallback:conflicting_evidence"] += 1

    write_decisions(con, run_month, decisions, stage="fallback")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS core.link_agreement (
            run_month VARCHAR, place_id VARCHAR, source VARCHAR, verdict VARCHAR, evidence VARCHAR)
        """
    )
    con.execute("DELETE FROM core.link_agreement WHERE run_month = ?", [run_month])
    if agreement:
        con.executemany(
            "INSERT INTO core.link_agreement VALUES (?, ?, ?, ?, ?)",
            [[run_month, *a] for a in agreement],
        )
    log.info("linking.fallback", **dict(sorted(stats.items())))
    return dict(stats)


def serp_targets(
    con: duckdb.DuckDBPyConnection,
    run_month: str,
    *,
    fallback_n: int,
    validation_n: int,
    min_reviews: int,
) -> list[tuple[str, str, str, str | None, str | None]]:
    """Places worth an Oxylabs query: busiest unresolved places first, plus a random sample of
    primary links (seeded by month, so the validation sample is reproducible)."""
    fallback = con.execute(
        """
        SELECT p.place_id, 'fallback', p.name, p.street, p.street_number
        FROM core.place_snapshot p JOIN core.place_entity_link l USING (run_month, place_id)
        WHERE p.run_month = ? AND p.in_scope AND l.status <> 'linked'
          AND coalesce(p.user_rating_count, 0) >= ? AND NOT p.self_service
        ORDER BY p.user_rating_count DESC LIMIT ?
        """,
        [run_month, min_reviews, fallback_n],
    ).fetchall()
    validation = con.execute(
        """
        SELECT p.place_id, 'validation', p.name, p.street, p.street_number
        FROM core.place_snapshot p JOIN core.place_entity_link l USING (run_month, place_id)
        WHERE p.run_month = ? AND p.in_scope AND l.status = 'linked' AND l.stage = 'primary'
        ORDER BY hash(p.place_id || ?) LIMIT ?
        """,
        [run_month, run_month, validation_n],
    ).fetchall()
    return [*fallback, *validation]
