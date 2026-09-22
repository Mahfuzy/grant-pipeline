"""Currency normalisation to ISO 4217 (SPEC §7).

A bare "$" is ambiguous: it is read as USD only when the funder is US-based or the
surrounding text says so; otherwise the result is flagged as ambiguous.
"""

import re
from dataclasses import dataclass

import pycountry

from fundscout.normalise.text import normalise_key

# Unambiguous symbols / prefixes, longest first so "US$" wins over "$".
_SYMBOLS: list[tuple[str, str]] = [
    ("US$", "USD"),
    ("USD$", "USD"),
    ("CA$", "CAD"),
    ("C$", "CAD"),
    ("AU$", "AUD"),
    ("A$", "AUD"),
    ("NZ$", "NZD"),
    ("GH₵", "GHS"),
    ("GH¢", "GHS"),
    ("₵", "GHS"),
    ("€", "EUR"),
    ("£", "GBP"),
    ("₦", "NGN"),
    ("₹", "INR"),
    ("KSh", "KES"),
    ("Ksh", "KES"),
]

# Currency words (matched on normalised text, whole words).
_WORDS: dict[str, str] = {
    "us dollar": "USD",
    "us dollars": "USD",
    "u s dollars": "USD",
    "american dollars": "USD",
    "euro": "EUR",
    "euros": "EUR",
    "pound sterling": "GBP",
    "pounds sterling": "GBP",
    "british pounds": "GBP",
    "naira": "NGN",
    "nigerian naira": "NGN",
    "cedi": "GHS",
    "cedis": "GHS",
    "ghana cedis": "GHS",
    "kenyan shilling": "KES",
    "kenyan shillings": "KES",
    "ugandan shillings": "UGX",
    "tanzanian shillings": "TZS",
    "south african rand": "ZAR",
    "rand": "ZAR",
    "swiss francs": "CHF",
    "canadian dollars": "CAD",
    "australian dollars": "AUD",
    "indian rupees": "INR",
}

_US_CONTEXT = re.compile(r"\b(U\.?S\.?A?|United States|federal)\b", re.IGNORECASE)


@dataclass(frozen=True)
class CurrencyResult:
    code: str | None
    ambiguous: bool = False


def is_iso_currency(code: str) -> bool:
    return pycountry.currencies.get(alpha_3=code.upper()) is not None


def normalise_currency(
    raw: str | None, *, context: str | None = None, funder_country: str | None = None
) -> CurrencyResult:
    """Map a currency code, symbol or word (or an amount string containing one) to ISO 4217."""
    if not raw or not raw.strip():
        return CurrencyResult(None)
    text = raw.strip()

    for code in re.findall(r"\b[A-Z]{3}\b", text):
        if is_iso_currency(code):
            return CurrencyResult(code)
    if re.fullmatch(r"[A-Za-z]{3}", text) and is_iso_currency(text):
        return CurrencyResult(text.upper())

    for symbol, code in _SYMBOLS:
        if symbol in text:
            return CurrencyResult(code)

    key = f" {normalise_key(text)} "
    for word, code in sorted(_WORDS.items(), key=lambda kv: -len(kv[0])):
        if f" {word} " in key:
            return CurrencyResult(code)

    if "$" in text or " dollar" in key:
        if funder_country == "US" or _US_CONTEXT.search(context or ""):
            return CurrencyResult("USD")
        return CurrencyResult(None, ambiguous=True)
    return CurrencyResult(None)
