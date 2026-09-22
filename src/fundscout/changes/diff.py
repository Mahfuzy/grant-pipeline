"""Field-level diffs for grant history (SPEC §8.4)."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

# Fields compared on every update. List fields are compared as sets.
SCALAR_FIELDS = (
    "title",
    "funder",
    "description",
    "amount_min",
    "amount_max",
    "currency",
    "amount_text",
    "opening_date",
    "closing_date",
    "deadline_type",
    "deadline_text",
    "eligibility_text",
    "application_url",
    "funder_page_url",
    "requirements",
    "funder_priorities",
    "contact_email",
    "contact_url",
)
LIST_FIELDS = (
    "documents_required",
    "countries",
    "regions",
    "org_types",
    "themes",
    "grant_types",
)
TAXONOMY_FIELDS = ("regions", "org_types", "themes", "grant_types")
TRACKED_FIELDS = SCALAR_FIELDS + LIST_FIELDS
# Disagreement on these between non-primary sources is a `conflicting_values` item.
CONFLICT_FIELDS = ("closing_date", "amount_min", "amount_max")


@dataclass(frozen=True)
class FieldChange:
    field: str
    old: Any
    new: Any


def to_json(value: Any) -> Any:
    """JSON-safe form used in grant_changes and review item details."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return format(value.normalize(), "f") if value == value.to_integral() else str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple, set)):
        return sorted(to_json(v) for v in value)
    return value


def is_empty(value: Any) -> bool:
    return value is None or value == "" or (isinstance(value, (list, tuple, set)) and not value)


def same(a: Any, b: Any) -> bool:
    if is_empty(a) and is_empty(b):
        return True
    return bool(to_json(a) == to_json(b))


def diff(old: dict[str, Any], new: dict[str, Any]) -> list[FieldChange]:
    """Changes for fields present in `new`, in TRACKED_FIELDS order."""
    return [
        FieldChange(name, to_json(old.get(name)), to_json(new[name]))
        for name in TRACKED_FIELDS
        if name in new and not same(old.get(name), new[name])
    ]
