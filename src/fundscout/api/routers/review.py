"""Review queue: GET /review, GET /review/{id}, POST /review/{id}/resolve|dismiss."""

import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from fundscout.api.deps import SessionDep
from fundscout.api.schemas import Page, ReviewAction, ReviewItemDetail, ReviewItemOut
from fundscout.api.serialise import review_detail, review_out
from fundscout.db.models import Grant, ReviewItem, ReviewReason, ReviewStatus
from fundscout.review.queue import close_review_item

router = APIRouter(prefix="/review", tags=["review"])


@router.get("", response_model=Page[ReviewItemOut])
def list_review_items(
    session: SessionDep,
    status_: ReviewStatus | None = Query(ReviewStatus.OPEN, alias="status"),
    reason: ReviewReason | None = None,
    grant_id: uuid.UUID | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> Page[ReviewItemOut]:
    query = select(ReviewItem, Grant.title).outerjoin(Grant, ReviewItem.grant_id == Grant.id)
    if status_:
        query = query.where(ReviewItem.status == status_)
    if reason:
        query = query.where(ReviewItem.reason == reason)
    if grant_id:
        query = query.where(ReviewItem.grant_id == grant_id)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = session.execute(
        query.order_by(ReviewItem.created_at.desc(), ReviewItem.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return Page(
        items=[review_out(item, title) for item, title in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


def _get(session: SessionDep, item_id: uuid.UUID) -> ReviewItem:
    item = session.get(ReviewItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "review item not found")
    return item


@router.get("/{item_id}", response_model=ReviewItemDetail)
def get_review_item(item_id: uuid.UUID, session: SessionDep) -> ReviewItemDetail:
    """The item with the grant, the source document and what was extracted from it."""
    return review_detail(session, _get(session, item_id))


def _close(
    session: SessionDep, item_id: uuid.UUID, action: ReviewAction, new_status: ReviewStatus
) -> ReviewItemDetail:
    item = _get(session, item_id)
    if item.status != ReviewStatus.OPEN:
        raise HTTPException(status.HTTP_409_CONFLICT, f"review item is already {item.status}")
    close_review_item(session, item, new_status, by=action.resolved_by, note=action.note)
    session.commit()
    return review_detail(session, item)


@router.post("/{item_id}/resolve", response_model=ReviewItemDetail)
def resolve(
    item_id: uuid.UUID, session: SessionDep, action: ReviewAction | None = None
) -> ReviewItemDetail:
    return _close(session, item_id, action or ReviewAction(), ReviewStatus.RESOLVED)


@router.post("/{item_id}/dismiss", response_model=ReviewItemDetail)
def dismiss(
    item_id: uuid.UUID, session: SessionDep, action: ReviewAction | None = None
) -> ReviewItemDetail:
    return _close(session, item_id, action or ReviewAction(), ReviewStatus.DISMISSED)
