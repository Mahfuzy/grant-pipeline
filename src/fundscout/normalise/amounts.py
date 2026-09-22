"""Amount parsing: "up to $50,000", "£10k–£25k", "USD 1.5 million" -> (min, max, currency)."""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from fundscout.normalise.currency import CurrencyResult, normalise_currency

_MULTIPLIERS = {
    "k": Decimal(1_000),
    "thousand": Decimal(1_000),
    "m": Decimal(1_000_000),
    "mn": Decimal(1_000_000),
    "mio": Decimal(1_000_000),
    "million": Decimal(1_000_000),
    "millions": Decimal(1_000_000),
    "bn": Decimal(1_000_000_000),
    "billion": Decimal(1_000_000_000),
}

_NUMBER = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:[,.   ]\d{3})+|\d+)([.,]\d+)?"
    r"(?:\s?(k|thousand|mn|mio|million|millions|m|bn|billion)\b)?",
    re.IGNORECASE,
)
_MAX_WORDS = re.compile(
    r"\b(up to|maximum|max\.?|not (?:to )?exceed(?:ing)?|no more than|ceiling|at most"
    r"|capped at)(?:\s+\w+){0,3}\s*$",
    re.IGNORECASE,
)
_MIN_WORDS = re.compile(
    r"\b(at least|minimum|min\.?|from|starting at|no less than)(?:\s+\w+){0,3}\s*$", re.I
)
_RANGE_JOINER = re.compile(r"^\s*(?:-|–|—|to|and|up to)\s*$", re.IGNORECASE)
_EMPTY = {"", "none", "n/a", "na", "null", "tbd", "unknown", "-"}


def _to_decimal(integer_part: str, fraction: str | None, multiplier: str | None) -> Decimal | None:
    digits = re.sub(r"[   ]", "", integer_part)
    seps = set(re.sub(r"\d", "", digits))
    if len(seps) > 1:
        return None  # mixed separators inside the integer part: unparseable
    digits = re.sub(r"[,.]", "", digits)
    number = digits
    if fraction:
        number += "." + fraction[1:]
    try:
        value = Decimal(number)
    except InvalidOperation:
        return None
    if multiplier:
        value *= _MULTIPLIERS[multiplier.lower()]
    return value


def parse_amount_value(raw: str | int | float | Decimal | None) -> Decimal | None:
    """A single amount: 50000, "50,000", "$1.5 million", "2 250 000". Non-positive -> None."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int | float | Decimal):
        value = Decimal(str(raw))
        return value if value > 0 else None
    if raw.strip().lower() in _EMPTY:
        return None
    match = _NUMBER.search(raw)
    if match is None:
        return None
    integer, fraction, multiplier = match.groups()
    # "1.500.000" or "1,500,000": separators are thousands. A single "." or "," followed
    # by exactly three digits and no multiplier is also read as a thousands separator.
    if fraction and not multiplier and len(fraction) == 4 and re.fullmatch(r"\d+", integer):
        integer, fraction = integer + fraction[1:], None
    parsed = _to_decimal(integer, fraction, multiplier)
    return parsed if parsed is not None and parsed > 0 else None


@dataclass(frozen=True)
class AmountRange:
    minimum: Decimal | None
    maximum: Decimal | None
    currency: CurrencyResult


def parse_amount_range(text: str | None, *, funder_country: str | None = None) -> AmountRange:
    """Min/max and currency from free text such as "up to $50,000" or "£10k–£25k"."""
    currency = normalise_currency(text, context=text, funder_country=funder_country)
    if not text:
        return AmountRange(None, None, currency)
    matches = [m for m in _NUMBER.finditer(text) if not _looks_like_year(m, text)]
    values = [parse_amount_value(m.group(0)) for m in matches]
    found = [(m, v) for m, v in zip(matches, values, strict=True) if v is not None]
    if not found:
        return AmountRange(None, None, currency)

    first_match, first = found[0]
    before = text[: first_match.start()]
    if len(found) >= 2:
        second_match, second = found[1]
        between = text[first_match.end() : second_match.start()]
        between = re.sub(r"[^\w\s\-–—]", " ", between)
        if _RANGE_JOINER.match(between):
            # Suffix multipliers apply to both ends: "£10–25k" -> 10,000–25,000.
            if first_match.group(3) is None and second_match.group(3) and first < second:
                first = parse_amount_value(first_match.group(0) + second_match.group(3)) or first
            low, high = sorted((first, second))
            return AmountRange(low, high, currency)
    if _MAX_WORDS.search(_strip_currency(before)):
        return AmountRange(None, first, currency)
    if _MIN_WORDS.search(_strip_currency(before)):
        return AmountRange(first, None, currency)
    return AmountRange(first, first, currency)


def _strip_currency(text: str) -> str:
    """Drop a currency marker and punctuation right before an amount ("Maximum: $")."""
    text = re.sub(r"(US\$|[A-Z]{3}|[$€£₦₵₹]|GH₵)\s*$", "", text.rstrip())
    return re.sub(r"[\s:=(]+$", "", text)


def _looks_like_year(match: re.Match[str], text: str) -> bool:
    token = match.group(0)
    if not re.fullmatch(r"(19|20)\d{2}", token):
        return False
    before = text[max(0, match.start() - 4) : match.start()]
    return not re.search(r"[$€£₦₵₹]|[A-Z]{3}\s?$", before)
