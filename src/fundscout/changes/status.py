"""Grant status rules (SPEC §6.3)."""

from datetime import date

from fundscout.db.models import DeadlineType, GrantStatus


def compute_status(
    opening_date: date | None,
    closing_date: date | None,
    deadline_type: DeadlineType | str,
    today: date,
) -> GrantStatus:
    """Rules in SPEC order: past closing date, future opening date, rolling, future
    closing date, otherwise unknown. A grant is still open on its closing date."""
    if closing_date is not None and closing_date < today:
        return GrantStatus.CLOSED
    if opening_date is not None and opening_date > today:
        return GrantStatus.UPCOMING
    if deadline_type == DeadlineType.ROLLING:
        return GrantStatus.ROLLING
    if closing_date is not None:
        return GrantStatus.OPEN
    return GrantStatus.UNKNOWN
