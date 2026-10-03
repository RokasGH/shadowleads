"""Trademark owners from the State Patent Bureau public database (search.linta.lt/pdb).

A registered brand ("7 Fridays", class 43) names its owner, e.g. UAB "Pikantiškas maistas". This is
evidence of the BRAND OWNER: for single-operator brands it is the operating company; for franchises
and groups that run one company per venue (e.g. Grill London: owner UAB "Kauno loftas", venues run
by separate UABs per VMVT) it is not. The fallback therefore treats a trademark-only link as weaker
than premises-level evidence and never lets it feed Priority-A leads on its own.

Politeness: one request per ~1.5 s, every response cached on disk, searches de-duplicated by term.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from selectolax.parser import HTMLParser

from shadowleads.http import PoliteClient
from shadowleads.linking.normalize import name_key
from shadowleads.log import get_logger

log = get_logger(__name__)

BASE = "https://search.linta.lt/pdb/trademark"
# Nice classes per category: 43 food & drink services, 41 entertainment, 44 hair/beauty,
# 37 vehicle repair & washing.
NICE_CLASSES = {"nightlife": {"43", "41"}, "hair_beauty": {"44"}, "auto": {"37"}}


@dataclass(slots=True)
class Mark:
    application_no: str
    text: str
    classes: set[str]
    status: str


@dataclass(slots=True)
class Owner:
    application_no: str
    mark: str
    owner_name: str
    owner_address: str


class TrademarkClient:
    def __init__(self, client: PoliteClient, cache_dir: Path):
        self.client = client
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)

    def _get(self, url: str) -> str:
        path = self.cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".html")
        if path.exists():
            return path.read_text(encoding="utf-8")
        resp = self.client.get(url)
        text = resp.content.decode("utf-8", errors="replace")
        path.write_text(text, encoding="utf-8")
        return text

    def search(self, term: str) -> list[Mark]:
        url = f"{BASE}/search?f540={quote(term)}&f540_mc=CONTAINS"
        marks = []
        for tr in HTMLParser(self._get(url)).css("tr"):
            link = tr.css_first("a[href*='/trademark/details/']")
            cells = [td.text(strip=True) for td in tr.css("td")]
            if not link or len(cells) < 9:
                continue
            marks.append(
                Mark(
                    application_no=cells[3],
                    text=cells[1],
                    classes={c.strip() for c in cells[6].split(",") if c.strip()},
                    status=cells[8],
                )
            )
        return marks

    def owner(self, application_no: str) -> Owner | None:
        html = self._get(f"{BASE}/details/{quote(application_no)}")
        tree = HTMLParser(html)
        lines = [
            ln.strip()
            for ln in (tree.body.text(separator="\n") if tree.body else "").splitlines()
            if ln.strip()
        ]
        mark = next(
            (lines[i + 1] for i, ln in enumerate(lines) if ln == "(540)" and i + 1 < len(lines)), ""
        )
        for i, line in enumerate(lines):
            # (732) = owner name and address (730/731 in older records)
            if re.fullmatch(r"\(73[0-2]\)", line):
                block = lines[i + 1 : i + 6]
                name = block[1] if len(block) > 1 and block[0].startswith("Pavadinimas") else ""
                addr = block[3] if len(block) > 3 and block[2].startswith("Adresas") else ""
                if name:
                    return Owner(application_no, mark, name, addr)
        return None


def search_term(place_name: str) -> str | None:
    """Most distinctive single token of the brand: the longest token of the core name."""
    tokens = [t for t in name_key(place_name).split() if len(t) >= 4 or t.isdigit()]
    return max(tokens, key=len) if tokens else None


def matching_owners(
    tm: TrademarkClient, place_name: str, category: str, max_details: int = 3
) -> list[Owner]:
    """Owners of registered marks whose distinctive words all appear in the Google name."""
    term = search_term(place_name)
    if not term:
        return []
    place_tokens = set(name_key(place_name).split())
    wanted = NICE_CLASSES.get(category, set())
    owners: list[Owner] = []
    for m in tm.search(term):
        mark_tokens = set(name_key(m.text).split())
        if (
            m.status.startswith("Registruotas")
            and m.classes & wanted
            and mark_tokens
            and mark_tokens <= place_tokens
        ):
            if (o := tm.owner(m.application_no)) is not None:
                owners.append(o)
            if len(owners) >= max_details:
                break
    return owners
