from shadowleads.sources.websites import extract_codes, skip_url


def codes(text: str) -> set[tuple[str, str]]:
    return {(t, c) for t, c, _ in extract_codes(text)}


def test_company_code_variants() -> None:
    assert ("company", "304227393") in codes("UAB „Roginta“, įmonės kodas 304227393, Vilnius")
    assert ("company", "304227393") in codes("Įm. k. 304227393")
    assert ("company", "123456789") in codes("Į.k.: 123456789")
    assert ("company", "302510268") in codes("Company code: 302510268")
    assert ("company", "110003978") in codes("Juridinio asmens kodas 110003978")


def test_vat_code() -> None:
    assert ("vat", "100010155711") in codes("PVM mokėtojo kodas LT100010155711")
    assert ("vat", "123456715") in codes("VAT: LT 123456715")


def test_phone_numbers_are_not_codes() -> None:
    assert codes("Tel. +370 612 34567, 8 612 34567") == set()
    assert codes("Užsakymo nr. 123456789") == set()


def test_platform_urls_are_skipped() -> None:
    assert skip_url("https://www.facebook.com/nomadsbar")
    assert skip_url("https://book.treatwell.lt/salonas/434231")
    assert not skip_url("https://baltasusas.lt/")
    assert skip_url(None)
