"""Name and address normalisation for linking Google listings to legal entities.

Pure functions only (property-tested). The same normalisation is applied to both sides of a match
so that "Bromas Baras" (Google) and VMI branch "Bromas" meet on the key `bromas`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import cache

from shadowleads.categories import CATEGORIES, COMMON_GENERIC_WORDS

# Longest first, so "uzdaroji akcine bendrove" is removed before "akcine bendrove".
_LEGAL_FORMS = sorted(
    (
        "uzdaroji akcine bendrove", "akcine bendrove", "mazoji bendrija", "individuali imone",
        "viesoji istaiga", "tikroji ukine bendrija", "komanditine ukine bendrija",
        "zemes ukio bendrove", "kooperatine bendrove", "zuvininkystes bendrove", "asociacija",
        "uab", "ab", "mb", "ii", "vsi", "tub", "kub", "zub", "ltd", "ou", "sia", "oy", "gmbh",
    ),
    key=len,
    reverse=True,
)  # fmt: skip
_LEGAL_FORM_RE = re.compile(r"\b(?:" + "|".join(re.escape(f) for f in _LEGAL_FORMS) + r")\b")
_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")
_SEPARATORS_RE = re.compile(r"\s*(?:\||/|,|;|–|—|\s-\s|\(|\)|:)\s*")


def fold(text: str | None) -> str:
    """Casefold, strip diacritics (ą->a, š->s, ...), keep only [a-z0-9] tokens separated by spaces."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    no_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_ALNUM_RE.sub(" ", no_marks).strip()


@cache
def generic_words() -> frozenset[str]:
    words = {fold(w) for c in CATEGORIES.values() for w in c.generic_words}
    words |= {fold(w) for w in COMMON_GENERIC_WORDS}
    return frozenset(w for w in words if w)


def strip_legal_form(folded: str) -> str:
    return re.sub(r"\s+", " ", _LEGAL_FORM_RE.sub(" ", folded)).strip()


def name_key(name: str | None) -> str:
    """Distinctive core of a business name: folded, legal forms and generic words removed.

    Returns "" when nothing distinctive is left (e.g. "Kirpykla", "UAB Baras") - such names must
    never create a link on their own.
    """
    tokens = [t for t in strip_legal_form(fold(name)).split() if t not in generic_words()]
    key = " ".join(tokens)
    return key if len(key.replace(" ", "")) >= 3 else ""


_CITY_WORDS = frozenset({"vilnius", "vilniuje", "vilniaus", "lt", "lietuva"})


def exact_key(name: str | None) -> str:
    """Full name with only the legal form, punctuation and city words removed.

    'Latransa autoservisas, UAB' == 'UAB Latransa autoservisas' -> 'latransa autoservisas'.
    Generic words are kept: this is the strict "full name" comparison.
    """
    tokens = [t for t in strip_legal_form(fold(name)).split() if t not in _CITY_WORDS]
    key = " ".join(tokens)
    return key if len(key.replace(" ", "")) >= 3 else ""


def name_variants(name: str | None) -> list[str]:
    """Keys for a Google display name and its separator-delimited parts.

    "Automobilių stiklai | GoGlass, UAB" -> ["automobiliu stiklai goglass", "automobiliu stiklai",
    "goglass"]. Order: full name first. Duplicates and empty keys removed.
    """
    if not name:
        return []
    parts = [name, *_SEPARATORS_RE.split(name)]
    seen: list[str] = []
    for part in parts:
        key = name_key(part)
        if key and key not in seen:
            seen.append(key)
    return seen


def quoted_brand(legal_name: str | None) -> str | None:
    """Brand inside quotes of a legal name: 'UAB "ROGINTA"' -> 'ROGINTA'."""
    if not legal_name:
        return None
    m = re.search(r"[\"„“”'](.+?)[\"„“”']", legal_name.replace('""', '"'))
    return m.group(1) if m else None


# ---------------------------------------------------------------- addresses

_STREET_TYPES = {
    "g": "g", "gatve": "g", "pr": "pr", "prospektas": "pr", "al": "al", "aleja": "al",
    "pl": "pl", "plentas": "pl", "skg": "skg", "skersgatvis": "skg", "a": "a", "aikste": "a",
    "kel": "kel", "kelias": "kel", "tak": "tak", "takas": "tak", "krant": "krant",
    "krantine": "krant", "skv": "skv", "skveras": "skv", "aklg": "aklg", "akligatvis": "aklg",
}  # fmt: skip
_STREET_RE = re.compile(
    r"^(?P<street>.+?)\s+(?P<type>"
    + "|".join(sorted(_STREET_TYPES, key=len, reverse=True))
    + r")\.?\s*(?P<num>\d+[a-z]?)?(?:\s*-\s*\d+[a-z]?)?\b",
)
_INITIAL_RE = re.compile(r"\b[a-z]\b\s*")


@dataclass(frozen=True, slots=True)
class Address:
    street: str
    street_type: str
    number: str | None

    @property
    def key(self) -> str | None:
        """'saviciaus|g|9' - None when there is no house number (too coarse to link on)."""
        return f"{self.street}|{self.street_type}|{self.number}" if self.number else None


def _street_core(raw: str) -> str:
    # Drop initials ("S. Stanevičiaus" -> "stanevičiaus"): Google often omits them.
    return re.sub(r"\s+", " ", _INITIAL_RE.sub(" ", raw)).strip()


def parse_street(street: str | None, number: str | None = None) -> Address | None:
    """Parse a street segment ("Savičiaus g. 9-12") or Google's route + street_number pair.

    Flat numbers ("-12") are dropped: the premises is the building, not the flat.
    """
    if not street:
        return None
    raw = _hyphenate(f"{street} {number or ''}")
    m = _STREET_RE.match(raw)
    if not m:
        tokens = raw.replace("-", " ").split()
        return (
            Address(_street_core(" ".join(tokens)), "", _clean_number(number)) if tokens else None
        )
    num = m.group("num") or _clean_number(number)
    return Address(_street_core(m.group("street")), _STREET_TYPES[m.group("type")], num)


def _hyphenate(raw: str) -> str:
    """fold() but keep '-' between numbers (flat numbers) so they can be dropped deliberately."""
    decomposed = unicodedata.normalize("NFKD", raw.casefold())
    no_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    no_marks = re.sub(r"[^0-9a-z\-]+", " ", no_marks)
    return re.sub(r"\s+", " ", no_marks).strip()


def _clean_number(number: str | None) -> str | None:
    if not number:
        return None
    m = re.match(r"\s*(\d+)\s*([a-zA-Z])?", fold(number).replace(" ", "") or "")
    if not m:
        return None
    return m.group(1) + (m.group(2) or "").lower()


def parse_lt_address(address: str | None) -> Address | None:
    """Parse a registry address such as 'Vilnius, S. Stanevičiaus g. 95, LT-07114' or
    'Vilniaus m. sav., Vilniaus m., Gedimino pr. 9-1'. Returns the street segment."""
    if not address:
        return None
    for segment in address.split(","):
        seg = segment.strip()
        if not seg or re.fullmatch(r"(LT-)?\d{5}", seg):
            continue
        parsed = parse_street(seg)
        if parsed and parsed.street_type and parsed.number:
            return parsed
    return None


def is_vilnius_city(address: str | None) -> bool:
    """Registry address is in Vilnius city (not Vilnius district, not another town).

    'Vilnius, Šaltkalvių g. 60A-245, LT-02175' -> True; 'Kaunas, M. K. Čiurlionio g. 4-7' ->
    False; 'Vilniaus r. sav., ...' -> False; 'Vilniaus m. sav., Vilniaus m., ...' -> True.
    """
    if not address:
        return False
    first = fold(address.split(",")[0])
    return first == "vilnius" or first.startswith("vilniaus m")


def is_vilnius_address(address: str | None) -> bool:
    folded = fold(address)
    return "vilniaus m" in folded or folded.startswith("vilnius") or " vilnius" in f" {folded}"
