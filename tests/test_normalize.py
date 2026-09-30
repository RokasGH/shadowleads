from hypothesis import given
from hypothesis import strategies as st

from shadowleads.linking.normalize import (
    fold,
    name_key,
    name_variants,
    parse_lt_address,
    parse_street,
    quoted_brand,
)

LT_LETTERS = "aąbcčdeęėfghiįyjklmnoprsštuųūvzž"
words = st.text(alphabet=LT_LETTERS, min_size=3, max_size=10)
names = st.lists(words, min_size=1, max_size=4).map(" ".join)


@given(st.text())
def test_fold_is_idempotent_and_ascii(text: str) -> None:
    once = fold(text)
    assert fold(once) == once
    assert set(once) <= set("abcdefghijklmnopqrstuvwxyz0123456789 ")


@given(names)
def test_name_key_ignores_case_diacritics_and_legal_form(name: str) -> None:
    key = name_key(name)
    assert name_key(name.upper()) == key
    assert name_key(f'UAB "{name}"') == key
    assert name_key(f"Uždaroji akcinė bendrovė „{name}“") == key
    assert name_key(f"{name}, MB") == key


@given(words, st.integers(1, 300), st.integers(1, 200))
def test_flat_number_is_dropped(street: str, house: int, flat: int) -> None:
    a = parse_street(f"{street.capitalize()} g. {house}-{flat}")
    b = parse_street(f"{street.capitalize()} g.", str(house))
    assert a is not None and b is not None
    assert a.key == b.key


def test_real_names() -> None:
    assert name_variants("Bromas Baras") == ["bromas"]
    assert name_key('Kavinė "Špunka"') == "spunka"
    # A different, longer brand must not collapse onto the shorter one.
    assert name_variants("Savičiaus Špunka") == ["saviciaus spunka"]
    assert name_variants("Automobilių stiklai | GoGlass, UAB")[-1] == "goglass"
    # Purely generic names never produce a key.
    assert name_variants("Kirpykla") == []
    assert name_key("UAB Baras") == ""
    assert quoted_brand('UAB ""Roginta""') == "Roginta"


def test_registry_addresses() -> None:
    a = parse_lt_address("Vilnius, S. Stanevičiaus g. 95, LT-07114")
    assert a is not None and a.key == "staneviciaus|g|95"
    b = parse_lt_address("Vilniaus m. sav., Vilniaus m., Gedimino pr. 9-1")
    assert b is not None and b.key == "gedimino|pr|9"
    # Google omits initials; registry keeps them - both must meet.
    g = parse_street("Stanevičiaus g.", "95")
    assert g is not None and g.key == a.key
    assert parse_street("Gedimino pr.", "44A").key == "gedimino|pr|44a"  # type: ignore[union-attr]
