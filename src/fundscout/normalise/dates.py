"""Date parsing to ISO dates (SPEC §7).

Numeric day/month order is taken from the source's locale ("DMY" or "MDY") when the
value itself is ambiguous (e.g. 03/04/2027); unambiguous values (31/03/2027) parse
regardless.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

DateOrder = Literal["DMY", "MDY"]

_MONTHS = {
    name: i
    for i, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))

_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_NUMERIC = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b")
_DAY_MONTH = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+of)?[\s\-]+({_MONTH_RE})\.?,?[\s\-]+(\d{{4}})\b", re.I
)
_MONTH_DAY = re.compile(
    rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.IGNORECASE
)


@dataclass(frozen=True)
class DateResult:
    value: date | None
    ambiguous: bool = False


def _make(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date(raw: str | date | None, order: DateOrder | None = None) -> DateResult:
    if raw is None:
        return DateResult(None)
    if isinstance(raw, date):
        return DateResult(raw)
    text = raw.strip()
    if not text:
        return DateResult(None)

    if m := _ISO.search(text):
        return DateResult(_make(int(m[1]), int(m[2]), int(m[3])))
    # Grants.gov "...Str" fields: 2026-11-17-00-00-00 is covered by _ISO above.
    if m := _DAY_MONTH.search(text):
        return DateResult(_make(int(m[3]), _MONTHS[m[2].lower()], int(m[1])))
    if m := _MONTH_DAY.search(text):
        return DateResult(_make(int(m[3]), _MONTHS[m[1].lower()], int(m[2])))
    if m := _NUMERIC.search(text):
        a, b, year = int(m[1]), int(m[2]), int(m[3])
        if a > 12 and b <= 12:
            return DateResult(_make(year, b, a))
        if b > 12 and a <= 12:
            return DateResult(_make(year, a, b))
        if a == b:
            return DateResult(_make(year, a, b))
        if order == "DMY":
            return DateResult(_make(year, b, a))
        if order == "MDY":
            return DateResult(_make(year, a, b))
        return DateResult(None, ambiguous=True)
    return DateResult(None)


def date_from_epoch_ms(value: str | int | None) -> date | None:
    """Calendar date from epoch milliseconds, read in UTC (the EU F&T Portal stores dates
    as UTC midnight)."""
    if value in (None, ""):
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC).date()
