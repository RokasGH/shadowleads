"""Primary linking: Google place -> legal entity by full name and/or address.

Rules, per place (first that fires wins):
  1. Exact full name - the Google name equals an entity's JAR legal name or VMI Vilnius branch
     trade name after removing only the legal form, punctuation and city words.
       unique entity -> HIGH if address agrees or activity (EVRK) fits the category, else MEDIUM
  2. Core name - same comparison after also removing generic words ("Bromas Baras" ~ "Bromas").
     Must be corroborated:
       address agrees                                   -> HIGH
       name is distinctive AND activity fits category   -> MEDIUM
       otherwise                                        -> ambiguous (sent to fallbacks)
  3. Name part ("GoGlass" in "Automobilių stiklai | GoGlass, UAB") + address agrees -> MEDIUM
  4. Address only - exactly one active, category-consistent entity registered at the address and
     the address is not multi-tenant -> MEDIUM
Several entities matching a name are narrowed by address, then by activity; if that does not leave
exactly one, the place is `ambiguous`. Fuzzy similarity never creates a link (evidence only).
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field

import duckdb
from rapidfuzz import fuzz

from shadowleads.categories import evrk_matches
from shadowleads.linking.normalize import (
    exact_key,
    is_vilnius_city,
    name_key,
    name_variants,
    parse_lt_address,
    parse_street,
)
from shadowleads.log import get_logger

log = get_logger(__name__)

# More registered entities than this at one address = business centre / virtual office.
MULTI_TENANT_ENTITIES = 5
# More Google places of our categories than this at one address = shopping centre, etc.
MULTI_TENANT_PLACES = 1
# A name token shared by more registered names than this is a dictionary word, not a brand.
DISTINCTIVE_MAX_DF = 8


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
    via: str  # exact_name | core_name | name_part | address
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
        self.by_exact: dict[str, set[tuple[int, str]]] = defaultdict(set)
        self.by_core: dict[str, set[tuple[int, str]]] = defaultdict(set)
        self.by_address: dict[str, set[int]] = defaultdict(set)
        self.token_df: Counter[str] = Counter()

        rows = con.execute(
            "SELECT ja_kodas, legal_name, registered_address, evrk_codes, vilnius_nexus "
            "FROM core.entity WHERE is_active"
        ).fetchall()
        for ja, legal_name, address, evrk_codes, nexus in rows:
            # Google places are all in Vilnius city: a registered address only counts as the same
            # premises when it is in Vilnius city too (audit: "Čiurlionio g. 4" matched Kaunas).
            addr = parse_lt_address(address) if is_vilnius_city(address) else None
            ref = EntityRef(
                ja, legal_name, addr.key if addr else None, tuple(evrk_codes or ()), nexus
            )
            self.entities[ja] = ref
            self.token_df.update(set(exact_key(legal_name).split()))
            if ref.address_key:
                self.by_address[ref.address_key].add(ja)
            if nexus:
                self._add_name(ja, legal_name, "jar_legal_name")

        for ja, branch_name in con.execute(
            "SELECT DISTINCT ja_kodas, branch_name FROM core.entity_branch "
            "WHERE in_vilnius AND is_current AND branch_name IS NOT NULL"
        ).fetchall():
            if ja in self.entities:
                self._add_name(ja, branch_name, "vmi_branch_name")
        log.info(
            "index.built",
            entities=len(self.entities),
            exact_keys=len(self.by_exact),
            core_keys=len(self.by_core),
            address_keys=len(self.by_address),
        )

    def _add_name(self, ja: int, name: str, source: str) -> None:
        if key := exact_key(name):
            self.by_exact[key].add((ja, source))
        if key := name_key(name):
            self.by_core[key].add((ja, source))

    def distinctive(self, key: str) -> bool:
        """A core key is distinctive if it has 2+ tokens or its token is rare among JAR names."""
        tokens = key.split()
        return len(tokens) >= 2 or all(self.token_df[t] <= DISTINCTIVE_MAX_DF for t in tokens)


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


def _narrow(
    grouped: dict[int, list[Candidate]], *, need_distinctive: bool, distinctive: bool
) -> tuple[int | None, str]:
    """Pick one entity out of several name matches: by address, then by activity."""
    by_addr = [ja for ja, cs in grouped.items() if any(c.address_agrees for c in cs)]
    if len(by_addr) == 1:
        return by_addr[0], "address"
    if by_addr:
        return None, f"{len(by_addr)} same-name entities at this address"
    if need_distinctive and not distinctive:
        return None, "generic name, no address agreement"
    by_evrk = [ja for ja, cs in grouped.items() if any(c.evrk_consistent for c in cs)]
    if len(by_evrk) == 1:
        return by_evrk[0], "activity"
    return None, f"{len(grouped)} entities share the name" if len(by_evrk) != 1 else ""


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

    def lookup(
        table: dict[str, set[tuple[int, str]]], keys: list[str], via: str
    ) -> list[Candidate]:
        return [
            _candidate(index, ja, src, k, via, category, address_key, name)
            for k in keys
            for ja, src in sorted(table.get(k, ()))
        ]

    # 1. exact full name
    exact = lookup(index.by_exact, [k for k in [exact_key(name)] if k], "exact_name")
    if exact:
        grouped = _by_entity(exact)
        if len(grouped) == 1:
            ((ja, cands),) = grouped.items()
            # audit: unique exact names still collide with unrelated companies ("Meistras ir
            # Margarita" = construction firm) - a name alone is not enough
            if any(c.address_agrees or c.evrk_consistent for c in cands):
                return LinkDecision(
                    place_id, "linked", ja, "exact_name", "HIGH",
                    "unique exact full-name match, corroborated", exact,
                )  # fmt: skip
            return LinkDecision(
                place_id, "ambiguous", reason="exact name, activity and address do not fit",
                candidates=exact,
            )  # fmt: skip
        ja, how = _narrow(grouped, need_distinctive=False, distinctive=True)
        if ja is not None:
            return LinkDecision(
                place_id, "linked", ja, f"exact_name+{how}", "HIGH",
                f"{len(grouped)} entities share the name; {how} singles one out", exact,
            )  # fmt: skip
        return LinkDecision(place_id, "ambiguous", reason=how, candidates=exact)

    # 2. core name (generic words removed) - needs corroboration
    core = lookup(index.by_core, variants[:1], "core_name")
    if core:
        grouped = _by_entity(core)
        distinctive = index.distinctive(variants[0])
        ja, how = _narrow(grouped, need_distinctive=True, distinctive=distinctive)
        if ja is not None:
            return LinkDecision(
                place_id, "linked", ja, f"core_name+{how}",
                "HIGH" if how == "address" else "MEDIUM",
                f"core-name match corroborated by {how}", core,
            )  # fmt: skip
        return LinkDecision(
            place_id, "ambiguous", reason=how or "core name not corroborated", candidates=core
        )

    # 3. name parts + address
    parts = lookup(index.by_core, variants[1:], "name_part")
    agreeing = {c.ja_kodas for c in parts if c.address_agrees}
    if len(agreeing) == 1:
        return LinkDecision(
            place_id, "linked", agreeing.pop(), "name_part+address", "MEDIUM",
            "name part matches and address agrees", parts,
        )  # fmt: skip

    # 4. address only
    addr_cands: list[Candidate] = []
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
        if consistent:
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
    return LinkDecision(place_id, "unmatched", reason=reason, candidates=addr_cands)


def run_primary_linking(con: duckdb.DuckDBPyConnection, run_month: str) -> dict[str, int]:
    index = EntityIndex(con)
    places = con.execute(
        "SELECT place_id, name, category, street, street_number "
        "FROM core.place_snapshot WHERE run_month = ? AND in_scope",
        [run_month],
    ).fetchall()
    keys = [(a.key if (a := parse_street(st, no)) else None) for *_, st, no in places]
    per_address = Counter(k for k in keys if k)
    decisions = [
        decide(index, pid, name, cat, st, no, per_address[k] if k else 0)
        for (pid, name, cat, st, no), k in zip(places, keys, strict=True)
    ]
    write_decisions(con, run_month, decisions, stage="primary")
    stats: Counter[str] = Counter(f"{d.status}:{d.method or d.reason}" for d in decisions)
    log.info("linking.primary", places=len(decisions), **dict(sorted(stats.items())))
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
