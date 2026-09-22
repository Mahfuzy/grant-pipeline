"""Normaliser tests covering the tricky formats in SPEC §7."""

from datetime import date
from decimal import Decimal

import pytest

from fundscout.normalise.amounts import parse_amount_range, parse_amount_value
from fundscout.normalise.countries import to_alpha2
from fundscout.normalise.currency import normalise_currency
from fundscout.normalise.dates import date_from_epoch_ms, parse_date
from fundscout.normalise.funders import normalise_funder_name
from fundscout.normalise.taxonomy import default_taxonomy
from fundscout.normalise.text import html_to_text, normalise_title, truncate

D = Decimal


# --- Currency -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("USD", "USD"),
        ("usd", "USD"),
        ("US dollars", "USD"),
        ("US$ 10,000", "USD"),
        ("€", "EUR"),
        ("EUR 2 250 000", "EUR"),
        ("euros", "EUR"),
        ("£10k", "GBP"),
        ("GH₵ 5,000", "GHS"),
        ("GH¢5,000", "GHS"),
        ("₦2,000,000", "NGN"),
        ("naira", "NGN"),
        ("Kenyan shillings", "KES"),
        ("KSh 100,000", "KES"),
    ],
)
def test_currency_unambiguous(raw: str, code: str) -> None:
    result = normalise_currency(raw)
    assert result.code == code and not result.ambiguous


def test_bare_dollar_is_ambiguous_without_us_context() -> None:
    assert normalise_currency("$50,000") == normalise_currency("$50,000", funder_country="GH")
    assert normalise_currency("$50,000").ambiguous
    assert normalise_currency("$50,000").code is None


def test_bare_dollar_is_usd_for_us_funder_or_context() -> None:
    assert normalise_currency("$50,000", funder_country="US").code == "USD"
    assert normalise_currency("$5,000", context="U.S. federal agency grant").code == "USD"


def test_currency_unknown_or_empty() -> None:
    assert normalise_currency(None).code is None
    assert normalise_currency("   ").code is None
    assert normalise_currency("grant funding").code is None
    assert not normalise_currency("grant funding").ambiguous


# --- Amounts ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "minimum", "maximum", "currency"),
    [
        ("up to $50,000", None, D(50000), None),  # "$" alone stays ambiguous
        ("up to US$50,000", None, D(50000), "USD"),
        ("£10k–£25k", D(10000), D(25000), "GBP"),
        ("£10–25k", D(10000), D(25000), "GBP"),
        ("USD 1.5 million", D(1500000), D(1500000), "USD"),
        ("between €100,000 and €500,000", D(100000), D(500000), "EUR"),
        ("EUR 2 250 000", D(2250000), D(2250000), "EUR"),
        ("€1.500.000", D(1500000), D(1500000), "EUR"),
        ("at least GH₵ 5,000", D(5000), None, "GHS"),
        ("Maximum grant: ₦2m", None, D(2000000), "NGN"),
        ("USD 20,000 - 50,000 per year for 2026", D(20000), D(50000), "USD"),
        ("Not stated", None, None, None),
    ],
)
def test_amount_ranges(
    text: str, minimum: Decimal | None, maximum: Decimal | None, currency: str | None
) -> None:
    result = parse_amount_range(text)
    assert (result.minimum, result.maximum, result.currency.code) == (minimum, maximum, currency)


def test_amount_range_uses_funder_country_for_dollar() -> None:
    assert parse_amount_range("up to $50,000", funder_country="US").currency.code == "USD"


@pytest.mark.parametrize(
    ("raw", "value"),
    [
        ("600000", D(600000)),
        ("2,250,000", D(2250000)),
        ("2,250,000.50", D("2250000.50")),
        (2250000, D(2250000)),
        (1.5, D("1.5")),
        ("1.5 million", D(1500000)),
        ("10K", D(10000)),
        ("none", None),  # Grants.gov uses "none" for a missing award ceiling
        ("0", None),
        ("", None),
        (None, None),
    ],
)
def test_amount_values(raw: str | int | float | None, value: Decimal | None) -> None:
    assert parse_amount_value(raw) == value


# --- Dates --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "order", "expected"),
    [
        ("31st March 2027", None, date(2027, 3, 31)),
        ("31 March 2027", None, date(2027, 3, 31)),
        ("Tuesday, 2nd of February 2027, 17:00 CET", None, date(2027, 2, 2)),
        ("March 31, 2027", None, date(2027, 3, 31)),
        ("Sept 1 2026", None, date(2026, 9, 1)),
        ("03/31/2027", None, date(2027, 3, 31)),  # unambiguous: 31 cannot be a month
        ("31/03/2027", None, date(2027, 3, 31)),
        ("31.03.2027", None, date(2027, 3, 31)),
        ("03/04/2027", "DMY", date(2027, 4, 3)),
        ("03/04/2027", "MDY", date(2027, 3, 4)),
        ("2027-03-31", None, date(2027, 3, 31)),
        ("2027-03-31T17:00:00+01:00", None, date(2027, 3, 31)),
        ("2026-11-17-00-00-00", None, date(2026, 11, 17)),  # Grants.gov *Str fields
        ("Nov 17, 2026 12:00:00 AM EST", None, date(2026, 11, 17)),
    ],
)
def test_dates(raw: str, order: str | None, expected: date) -> None:
    assert parse_date(raw, order).value == expected  # type: ignore[arg-type]


def test_ambiguous_numeric_date_without_locale() -> None:
    result = parse_date("03/04/2027")
    assert result.value is None and result.ambiguous


@pytest.mark.parametrize("raw", ["", "rolling", "31 February 2027", "TBC", None])
def test_unparseable_dates(raw: str | None) -> None:
    result = parse_date(raw)
    assert result.value is None and not result.ambiguous


def test_epoch_ms() -> None:
    assert date_from_epoch_ms("1788307200000") == date(2026, 9, 2)  # 2026-09-02T00:00Z
    assert date_from_epoch_ms(None) is None


# --- Countries, funders, text -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("Ghana", "GH"),
        ("gh", "GH"),
        ("NGA", "NG"),
        ("Côte d'Ivoire", "CI"),
        ("Ivory Coast", "CI"),
        ("DRC", "CD"),
        ("Democratic Republic of the Congo", "CD"),
        ("Tanzania", "TZ"),
        ("United Kingdom", "GB"),
        ("UK", "GB"),
        ("USA", "US"),
        ("Atlantis", None),
        ("", None),
    ],
)
def test_countries(raw: str, code: str | None) -> None:
    assert to_alpha2(raw) == code


@pytest.mark.parametrize(
    ("name", "normalised"),
    [
        ("The Bill & Melinda Gates Foundation", "bill melinda gates"),
        ("Wellcome Trust", "wellcome"),
        ("Acme Philanthropies, Inc.", "acme philanthropies"),
        ("Fondation Botnar", "fondation botnar"),
        ("Global Health EDCTP3 Joint Undertaking", "global health edctp3 joint undertaking"),
        ("Foundation", "foundation"),  # never strip to nothing
        ("National Institutes of Health (NIH)", "national institutes of health"),
        ("(NIH)", "nih"),
    ],
)
def test_funder_names(name: str, normalised: str) -> None:
    assert normalise_funder_name(name) == normalised


def test_text_helpers() -> None:
    assert html_to_text("<p>One&nbsp;two</p><p><strong>Three</strong></p>") == "One two\nThree"
    assert normalise_title("  Health: Innovation Fund (2027)! ") == "health innovation fund 2027"
    assert truncate("First sentence. Second sentence here.", 25) == "First sentence."
    assert truncate("short", 25) == "short"


# --- Taxonomy -----------------------------------------------------------------------------


def test_taxonomy_resolves_slugs_labels_and_aliases() -> None:
    tax = default_taxonomy()
    assert tax.resolve("themes", "health") == ["health"]
    assert tax.resolve("themes", "Climate and Environment") == ["climate-environment"]
    assert tax.resolve("themes", "Natural Resources") == ["climate-environment"]
    assert set(
        tax.resolve("themes", "science and technology and other research and development")
    ) == {"research", "technology-innovation"}
    assert tax.resolve("org_types", "Small businesses") == ["sme-for-profit"]
    assert tax.resolve("regions", "Western Africa") == ["west-africa"]
    assert tax.resolve("regions", "Sub-Saharan Africa") == ["sub-saharan-africa"]
    assert tax.resolve("themes", "Housing") == []
    assert "GH" in tax.region_countries["west-africa"]
