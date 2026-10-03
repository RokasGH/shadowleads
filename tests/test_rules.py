"""Regression tests for rules introduced after the manual link audit."""

from shadowleads.linking.normalize import is_vilnius_city, parse_lt_address, parse_street
from shadowleads.linking.resolve import company_names_in
from shadowleads.sources.trademarks import search_term


def test_registered_apartment_matches_premises() -> None:
    # audit: MB Carsfera - premises Šaltkalvių g. 60A, company registered in flat 245 of it
    reg = parse_lt_address("Vilnius, Šaltkalvių g. 60A-245, LT-02175")
    google = parse_street("Šaltkalvių g.", "60A")
    assert reg is not None and google is not None and reg.key == google.key


def test_same_street_in_another_city_is_not_vilnius() -> None:
    # audit: "Nagų ikona" matched a company at M. K. Čiurlionio g. 4 - in Kaunas
    assert not is_vilnius_city("Kaunas, M. K. Čiurlionio g. 4-7, LT-44360")
    assert not is_vilnius_city("Vilniaus r. sav., Avižienių sen., Ežeraičių g. 2")
    assert is_vilnius_city("Vilnius, Šaltkalvių g. 60A-245, LT-02175")
    assert is_vilnius_city("Vilniaus m. sav., Vilniaus m., Gedimino pr. 9-1")


def test_company_names_in_job_ads() -> None:
    assert "Pikantiškas maistas" in company_names_in(
        "UAB „Pikantiškas maistas“, Vilnius, rugsėjo 01 d. Plečiantis 7 Fridays barų tinklui"
    )
    assert "Kauno loftas" in company_names_in(
        'restorane "Grill London". UAB „Kauno loftas“, Vilnius'
    )
    assert "Bromas" in company_names_in("Bromas, UAB rekvizitai ir kontaktai")


def test_trademark_search_term_is_distinctive() -> None:
    assert search_term("7 Fridays Vingis") == "fridays"
    assert search_term("Kirpykla") is None
