"""Business categories: Google primary place types <-> EVRK activity codes.

EVRK is used as *validation evidence* for a link, never as a hard filter (many companies keep an
outdated or unrelated main activity code). Lithuania moved from EVRK 2 to EVRK 2.1 (NACE Rev. 2.1)
in 2025, so both code families are listed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Category:
    key: str
    label: str
    google_primary_types: tuple[str, ...]
    # EVRK prefixes in dotted form ("56.30"); matched with startswith on e.g. "56.30.10".
    evrk_prefixes: tuple[str, ...]
    # Words that describe the category rather than the business (stripped before name matching).
    generic_words: tuple[str, ...]
    # Sub-types Google returns for the requested types (e.g. gastropub under bar). Used only to
    # classify results - not sent in requests, so the response cache stays valid.
    also_classify: tuple[str, ...] = ()


CATEGORIES: dict[str, Category] = {
    c.key: c
    for c in (
        Category(
            key="hair_beauty",
            label="Hairdressers & beauty salons",
            google_primary_types=(
                "hair_salon", "barber_shop", "beauty_salon", "nail_salon", "hair_care", "beautician",
            ),
            evrk_prefixes=("96.02", "96.21", "96.22", "96.23"),
            generic_words=(
                "kirpykla", "kirpyklos", "kirpėjas", "kirpeja", "barbershop", "barber", "shop",
                "grozio", "salonas", "salon", "studija", "studio", "nagu", "manikiuro", "beauty",
                "hair", "nails", "spa", "kosmetologija", "kosmetologijos", "centras",
            ),
        ),
        Category(
            key="nightlife",
            label="Bars, pubs & night clubs",
            google_primary_types=(
                "bar", "pub", "night_club", "cocktail_bar", "wine_bar", "lounge_bar", "sports_bar",
                "karaoke", "dance_hall", "bar_and_grill",
            ),
            evrk_prefixes=("56.30", "56.10", "56.11", "56.12", "56.21", "56.29", "93.29", "93.21"),
            generic_words=(
                "baras", "bar", "baaras", "pub", "pubas", "klubas", "club", "naktinis", "night",
                "kokteiliu", "cocktail", "vyno", "wine", "alaus", "beer", "restoranas", "restaurant",
                "kavine", "cafe", "lounge", "karaoke", "bistro", "grill",
            ),
            also_classify=("gastropub", "irish_pub", "brewpub", "beer_garden", "hookah_bar"),
        ),
        Category(
            key="auto",
            label="Car wash & car repair",
            google_primary_types=("car_wash", "car_repair"),
            evrk_prefixes=("45.20", "95.31", "95.32"),
            generic_words=(
                "autoservisas", "autoserviso", "servisas", "service", "auto", "automobiliu",
                "plovykla", "plovimas", "savitarnos", "savitarna", "car", "wash", "remontas",
                "padangos", "padangu", "kebulu", "dazymas", "vilniuje", "vilnius", "centras",
            ),
            also_classify=("tire_shop",),
        ),
    )
}  # fmt: skip

# Self-service car washes legitimately run with ~no staff: tagged and excluded from scoring.
SELF_SERVICE_MARKERS = ("savitarn", "self service", "self-service", "selfservice", "bekontakt")

# Words that are never distinctive in a business name, regardless of category.
COMMON_GENERIC_WORDS = (
    "uab", "mb", "ii", "vsi", "ab", "lt", "vilnius", "vilniuje", "vilniaus", "lietuva",
    "the", "and", "ir", "de", "la", "by", "nr", "g",
    # VMI branch descriptors ("Biuras", "Parduotuvė", ...) that are not business names
    "biuras", "parduotuve", "dirbtuves", "garazas", "cechas", "kabinetas", "paviljonas", "namai",
    "patalpa", "patalpos", "sandelis", "padalinys", "filialas", "gamybine", "prekybos", "vieta",
    "taskas", "kioskas", "remonto", "paslaugu", "paslaugos", "buveine", "administracija",
    "autoplovykla", "gamyba", "cechas", "biuro", "pastatas", "salone",
)  # fmt: skip


def category_for_primary_type(primary_type: str | None) -> str | None:
    if not primary_type:
        return None
    for cat in CATEGORIES.values():
        if primary_type in cat.google_primary_types or primary_type in cat.also_classify:
            return cat.key
    return None


def evrk_matches(category_key: str, evrk: str | None) -> bool:
    """`evrk` may be dotted ("56.30.00") or compact ("563000"); both are accepted."""
    if not evrk:
        return False
    code = evrk.strip()
    if "." not in code and code.isdigit() and len(code) >= 4:
        code = f"{code[:2]}.{code[2:4]}{'.' + code[4:] if len(code) > 4 else ''}"
    return any(code.startswith(p) for p in CATEGORIES[category_key].evrk_prefixes)
