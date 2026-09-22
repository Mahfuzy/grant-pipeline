"""Review queue (SPEC §8.3): create items without piling up duplicates, keep
`grants.needs_review` in sync."""

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from fundscout.db.models import Grant, ReviewItem, ReviewReason, ReviewStatus

log = structlog.get_logger(__name__)


def open_review_item(
    session: Session,
    reason: ReviewReason,
    *,
    grant: Grant | None = None,
    raw_document_id: Any = None,
    details: dict[str, Any] | None = None,
    dedupe_on: dict[str, Any] | None = None,
    dedupe: bool = True,
) -> ReviewItem | None:
    """Create an open review item unless an equivalent one is already open.

    Equivalent: same reason and grant (or, without a grant, same raw document) and the
    same values for the `dedupe_on` detail keys. Returns None when deduplicated.
    """
    details = details or {}
    if dedupe:
        query = select(ReviewItem).where(
            ReviewItem.reason == reason, ReviewItem.status == ReviewStatus.OPEN
        )
        if grant is not None:
            query = query.where(ReviewItem.grant_id == grant.id)
        elif raw_document_id is not None:
            query = query.where(ReviewItem.raw_document_id == raw_document_id)
        for key, value in (dedupe_on or {}).items():
            query = query.where(ReviewItem.details[key].astext == str(value))
        if session.scalars(query.limit(1)).first() is not None:
            return None
    item = ReviewItem(
        grant_id=grant.id if grant is not None else None,
        raw_document_id=raw_document_id,
        reason=reason,
        details=details,
    )
    session.add(item)
    if grant is not None:
        grant.needs_review = True
    log.info("review item opened", reason=reason.value, grant_id=str(grant.id) if grant else None)
    return item


def refresh_needs_review(session: Session, grant: Grant) -> None:
    session.flush()
    grant.needs_review = bool(
        session.scalar(
            select(
                exists().where(
                    ReviewItem.grant_id == grant.id, ReviewItem.status == ReviewStatus.OPEN
                )
            )
        )
    )


def close_review_item(
    session: Session,
    item: ReviewItem,
    status: ReviewStatus,
    *,
    by: str | None,
    note: str | None,
) -> ReviewItem:
    item.status = status
    item.resolved_by = by
    item.resolution_note = note
    item.resolved_at = datetime.now(UTC)
    if item.grant_id is not None:
        grant = session.get(Grant, item.grant_id)
        if grant is not None:
            refresh_needs_review(session, grant)
    return item
