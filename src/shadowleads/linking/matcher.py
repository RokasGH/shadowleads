"""Primary linking: Google place -> legal entity by full name and/or address.

Rules (in order, per place):
  1. Full-name match: the place's normalised full name equals the key of exactly one entity
     (VMI Vilnius branch trade name or JAR legal-name core).
       - registered/premises address agrees  -> HIGH
       - no address agreement                -> MEDIUM
     Several entities share the name -> keep those whose address agrees; exactly one -> HIGH.
  2. Name-part match (a separator-delimited part of the Google name, e.g. "GoGlass" in
     "Automobilių stiklai | GoGlass, UAB"): exactly one entity AND address agrees -> MEDIUM.
  3. Address-only: exactly one active, category-consistent entity at that address, and the
     address is not a multi-tenant hotspot -> MEDIUM.
  Otherwise the place is left `ambiguous` / `unmatched` for the fallback stage.

Fuzzy similarity never creates a link; it is only recorded as evidence for validation.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field

import duckdb
from rapidfuzz import fuzz

from shadowleads.categories import evrk_matches
from shadowleads.linking.normalize import name_key, name_variants, parse_lt_address, parse_street
from shadowleads.log import get_logger

log = get_logger(__name__)

# More registered entities than this at one address = business centre / virtual office.
MULTI_TENANT_ENTITIES = 5
# More Google places of our categories than this at one address = shopping centre, etc.
MULTI_TENANT_PLACES = 1


@dataclass(slots=True)
class EntityRef:
    ja_kodas: int
    legal_name: str
    address_key: str | None
    evrk_codes: tuple[str, ...]
    vilnius_nexus: bool


@dataclass(slots=True)
class Candidate:
    ja_kodas: int
    source: str  # vmi_branch_name | jar_legal_name | jar_address
    matched_key: str
    via: str  # full_name | name_part | address
    address_agrees: bool = False
    evrk_consistent: bool = False
    vilnius_nexus: bool = False
    name_similarity: float = 0.0


@dataclass(slots=True)
class LinkDecision:
    place_id: str
    status: str  # linked | ambiguous | unmatched
    ja_kodas: int | None = None
    method: str | None = None
    confidence: str | None = None  # HIGH | MEDIUM
    reason: str = ""
    candidates: list[Candidate] = field(default_factory=list)


class EntityIndex:
    """In-memory lookup structures built from the staged registries."""

    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.entities: dict[int, EntityRef] = {}
        self.by_name: dict[str, set[tuple[int, str]]] = defaultdict(set)
        self.by_address: dict[str, set[int]] = defaultdict(set)

        rows = con.execute(
            """
            SELECT ja_kodas, legal_name, registered_address, evrk_codes, vilnius_nexus
            FROM core.entity WHERE is_active
            """
        ).fetchall()
        for ja, legal_name, address, evrk_codes, nexus in rows:
            addr = parse_lt_address(address)
            ref = EntityRef(
                ja, legal_name, addr.key if addr else None, tuple(evrk_codes or ()), nexus
            )
            self.entities[ja] = ref
            if ref.address_key:
                self.by_address[ref.address_key].add(ja)
            if nexus:
                key = name_key(legal_name)
                if key:
                    self.by_name[key].add((ja, "jar_legal_name"))

        for ja, branch_name in con.execute(
            "SELECT DISTINCT ja_kodas, branch_name FROM core.entity_branch "
            "WHERE in_vilnius AND is_current AND branch_name IS NOT NULL"
        ).fetchall():
            key = name_key(branch_name)
            if key and ja in self.entities:
                self.by_name[key].add((ja, "vmi_branch_name"))
        log.info(
            "index.built",
            entities=len(self.entities),
            name_keys=len(self.by_name),
            address_keys=len(self.by_address),
        )


def _candidate(
    index: EntityIndex, ja: int, source: str, key: str, via: str, category: str,
    address_key: str | None, place_name: str,
) -> Candidate:  # fmt: skip
    ref = index.entities[ja]
    return Candidate(
        ja_kodas=ja,
        source=source,
        matched_key=key,
        via=via,
        address_agrees=bool(address_key and ref.address_key == address_key),
        evrk_consistent=any(evrk_matches(category, c) for c in ref.evrk_codes),
        vilnius_nexus=ref.vilnius_nexus,
        name_similarity=round(
            fuzz.token_set_ratio(name_key(place_name), name_key(ref.legal_name)), 1
        ),
    )


def _by_entity(cands: list[Candidate]) -> dict[int, list[Candidate]]:
    out: dict[int, list[Candidate]] = defaultdict(list)
    for c in cands:
        out[c.ja_kodas].append(c)
    return out


def decide(
    index: EntityIndex,
    place_id: str,
    name: str,
    category: str,
    street: str | None,
    number: str | None,
    places_at_address: int,
) -> LinkDecision:
    addr = parse_street(street, number)
    address_key = addr.key if addr else None
    variants = name_variants(name)

    def lookup(keys: list[str], via: str) -> list[Candidate]:
        return [
            _candidate(index, ja, src, k, via, category, address_key, name)
            for k in keys
            for ja, src in sorted(index.by_name.get(k, ()))
        ]

    # 1. full name
    full = lookup(variants[:1], "full_name")
    if full:
        grouped = _by_entity(full)
        if len(grouped) == 1:
            ((ja, cands),) = grouped.items()
            agrees = any(c.address_agrees for c in cands)
            return LinkDecision(
                place_id, "linked", ja, "name_full" + ("+address" if agrees else ""),
                "HIGH" if agrees else "MEDIUM", "unique full-name match", full,
            )  # fmt: skip
        agreeing = {ja for ja, cs in grouped.items() if any(c.address_agrees for c in cs)}
        if len(agreeing) == 1:
            ja = agreeing.pop()
            return LinkDecision(
                place_id, "linked", ja, "name_full+address", "HIGH",
                f"{len(grouped)} entities share the name; address singles one out", full,
            )  # fmt: skip
        return LinkDecision(
            place_id,
            "ambiguous",
            reason=f"{len(grouped)} entities share the full name",
            candidates=full,
        )

    # 2. name parts
    parts = lookup(variants[1:], "name_part")
    if parts:
        grouped = _by_entity(parts)
        agreeing = {ja for ja, cs in grouped.items() if any(c.address_agrees for c in cs)}
        if len(agreeing) == 1:
            ja = agreeing.pop()
            return LinkDecision(
                place_id, "linked", ja, "name_part+address", "MEDIUM",
                "name part matches and address agrees", parts,
            )  # fmt: skip

    # 3. address only
    if address_key:
        at_address = sorted(index.by_address.get(address_key, ()))
        addr_cands = [
            _candidate(
                index, ja, "jar_address", address_key, "address", category, address_key, name
            )
            for ja in at_address
        ]
        consistent = [c for c in addr_cands if c.evrk_consistent]
        hotspot = len(at_address) > MULTI_TENANT_ENTITIES or places_at_address > MULTI_TENANT_PLACES
        if len(consistent) == 1 and not hotspot:
            c = consistent[0]
            return LinkDecision(
                place_id, "linked", c.ja_kodas, "address_only", "MEDIUM",
                "only category-consistent entity registered at this address", parts + addr_cands,
            )  # fmt: skip
        if parts or addr_cands:
            why = (
                "multi-tenant address"
                if hotspot
                else f"{len(consistent)} consistent entities at address"
            )
            return LinkDecision(place_id, "ambiguous", reason=why, candidates=parts + addr_cands)

    if parts:
        return LinkDecision(
            place_id, "ambiguous", reason="name part without address", candidates=parts
        )
    reason = "no distinctive name" if not variants else "no candidate entity"
    return LinkDecision(place_id, "unmatched", reason=reason)


def run_primary_linking(con: duckdb.DuckDBPyConnection, run_month: str) -> dict[str, int]:
    index = EntityIndex(con)
    places = con.execute(
        """
        SELECT place_id, name, category, street, street_number,
               count(*) OVER (PARTITION BY lower(street), lower(street_number)) AS places_at_address
        FROM core.place_snapshot WHERE run_month = ? AND in_scope
        """,
        [run_month],
    ).fetchall()
    decisions = [decide(index, *row) for row in places]
    write_decisions(con, run_month, decisions, stage="primary")
    stats: dict[str, int] = defaultdict(int)
    for d in decisions:
        stats[f"{d.status}:{d.method or d.reason}"] += 1
    log.info("linking.primary", places=len(decisions), **{k: v for k, v in sorted(stats.items())})
    return dict(stats)


LINK_DDL = """
CREATE TABLE IF NOT EXISTS core.match_candidate (
    run_month VARCHAR, stage VARCHAR, place_id VARCHAR, ja_kodas BIGINT, source VARCHAR,
    matched_key VARCHAR, via VARCHAR, address_agrees BOOLEAN, evrk_consistent BOOLEAN,
    vilnius_nexus BOOLEAN, name_similarity DOUBLE
);
CREATE TABLE IF NOT EXISTS core.place_entity_link (
    run_month VARCHAR, place_id VARCHAR, status VARCHAR, ja_kodas BIGINT, method VARCHAR,
    confidence VARCHAR, stage VARCHAR, reason VARCHAR, evidence JSON,
    PRIMARY KEY (run_month, place_id)
);
"""


def write_decisions(
    con: duckdb.DuckDBPyConnection, run_month: str, decisions: list[LinkDecision], *, stage: str
) -> None:
    con.execute(LINK_DDL)
    ids = [d.place_id for d in decisions]
    if not ids:
        return
    con.execute(
        "DELETE FROM core.match_candidate WHERE run_month = ? AND stage = ? AND place_id IN "
        "(SELECT unnest(?::VARCHAR[]))",
        [run_month, stage, ids],
    )
    con.executemany(
        "INSERT INTO core.match_candidate VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            [run_month, stage, d.place_id, c.ja_kodas, c.source, c.matched_key, c.via,
             c.address_agrees, c.evrk_consistent, c.vilnius_nexus, c.name_similarity]
            for d in decisions
            for c in d.candidates
        ],
    )  # fmt: skip
    con.executemany(
        "INSERT OR REPLACE INTO core.place_entity_link VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            [run_month, d.place_id, d.status, d.ja_kodas, d.method, d.confidence, stage, d.reason,
             json.dumps([asdict(c) for c in d.candidates if c.ja_kodas == d.ja_kodas] or None)]
            for d in decisions
        ],
    )  # fmt: skip
