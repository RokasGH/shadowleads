"""Resolve a company *name* found in free text (trademark owner, job-ad employer) to a JAR code.

Only exact matches of the normalised legal name (legal form removed) among active entities count,
and only when the name is unique in the register - otherwise the name is not evidence.
"""

from __future__ import annotations

import re
from collections import defaultdict

import duckdb

from shadowleads.linking.normalize import exact_key

LEGAL_FORMS = r"(?:UAB|MB|IĮ|VšĮ|AB)"
_QUOTED = re.compile(LEGAL_FORMS + r"\s*[„\"“']\s*([^\"“”„']{2,60}?)\s*[“\"”']")
_SUFFIX = re.compile(r"([A-ZĄČĘĖĮŠŲŪŽ0-9][^,.;|„“\"()]{1,50}?),\s*" + LEGAL_FORMS + r"\b")
_PREFIX = re.compile(LEGAL_FORMS + r"\s+([A-ZĄČĘĖĮŠŲŪŽ0-9][^,.;|„“\"()\-–]{1,50})")
_STOP = {"siūlo", "siulo", "ieško", "iesko", "darbo", "darbą", "darba", "kviečia", "vilnius",
         "vilniuje", "kaunas", "pasiūlymai", "pasiulymai", "skelbimai", "prieš"}  # fmt: skip


def company_names_in(text: str) -> set[str]:
    """Candidate company names mentioned with a legal form: 'UAB „Kauno loftas“',
    'Pikantiškas maistas, UAB', 'UAB RESTORANO PROJEKTAS siūlo ...'."""
    names = {m.group(1) for m in _QUOTED.finditer(text)}
    names |= {m.group(1) for m in _SUFFIX.finditer(text)}
    for m in _PREFIX.finditer(text):
        words = []
        for w in m.group(1).split():
            if w.lower().strip(":") in _STOP:
                break
            words.append(w)
        if words:
            names.add(" ".join(words[:4]))
    return {n.strip() for n in names if len(n.strip()) >= 3}


class NameResolver:
    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.by_name: dict[str, set[int]] = defaultdict(set)
        for ja, name in con.execute(
            "SELECT ja_kodas, legal_name FROM core.entity WHERE is_active"
        ).fetchall():
            if key := exact_key(name):
                self.by_name[key].add(ja)

    def resolve(self, name: str) -> int | None:
        """Unique active entity with this exact legal name; tries shorter prefixes for
        unquoted names that may have trailing words ('Vilniaus režisierius siūlo' -> ...)."""
        tokens = exact_key(name).split()
        for n in range(len(tokens), 0, -1):
            if n < len(tokens) and n < 2:  # never shorten to a single (likely generic) word
                break
            hits = self.by_name.get(" ".join(tokens[:n]), set())
            if len(hits) == 1:
                return next(iter(hits))
            if len(hits) > 1:
                return None
        return None
