from datetime import date
from decimal import Decimal

import pytest

from fundscout.changes.diff import diff, to_json
from fundscout.changes.status import compute_status
from fundscout.db.models import GrantStatus

TODAY = date(2026, 9, 21)


@pytest.mark.parametrize(
    ("opening", "closing", "deadline_type", "expected"),
    [
        (None, date(2026, 9, 20), "fixed", GrantStatus.CLOSED),
        (None, TODAY, "fixed", GrantStatus.OPEN),  # still open on the closing date
        (date(2026, 10, 1), date(2026, 12, 1), "fixed", GrantStatus.UPCOMING),
        (None, None, "rolling", GrantStatus.ROLLING),
        (None, date(2026, 1, 1), "rolling", GrantStatus.CLOSED),  # past date wins
        (date(2026, 1, 1), date(2026, 12, 1), "multiple", GrantStatus.OPEN),
        (date(2026, 1, 1), None, "unknown", GrantStatus.UNKNOWN),
        (None, None, "unknown", GrantStatus.UNKNOWN),
    ],
)
def test_compute_status(
    opening: date | None, closing: date | None, deadline_type: str, expected: GrantStatus
) -> None:
    assert compute_status(opening, closing, deadline_type, TODAY) == expected


def test_diff_normalises_values() -> None:
    old = {
        "amount_max": Decimal("50000.00"),
        "closing_date": date(2026, 10, 1),
        "themes": ["health", "research"],
        "description": None,
    }
    new = {
        "amount_max": Decimal("50000"),  # same number
        "closing_date": date(2026, 11, 1),
        "themes": ["research", "health"],  # same set
        "description": "",  # empty == None
        "title": "New title",
    }
    changes = diff(old, new)
    assert [(c.field, c.old, c.new) for c in changes] == [
        ("title", None, "New title"),
        ("closing_date", "2026-10-01", "2026-11-01"),
    ]


def test_to_json() -> None:
    assert to_json(Decimal("1500000.00")) == "1500000"
    assert to_json(Decimal("12.50")) == "12.50"
    assert to_json({"b", "a"}) == ["a", "b"]
