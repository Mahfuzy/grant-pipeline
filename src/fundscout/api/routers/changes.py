"""GET /changes?since= (SPEC §11)."""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from fundscout.api.deps import SessionDep
from fundscout.api.schemas import ChangeOut, Page
from fundscout.api.serialise import change_out
from fundscout.db.models import Grant, GrantChange

router = APIRouter(prefix="/changes", tags=["changes"])


@router.get("", response_model=Page[ChangeOut])
def list_changes(
    session: SessionDep,
    since: datetime | None = Query(None, description="Default: 7 days ago"),
    field: str | None = None,
    grant_id: uuid.UUID | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
) -> Page[ChangeOut]:
    since = since or datetime.now(UTC) - timedelta(days=7)
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    query = select(GrantChange, Grant.title).join(Grant).where(GrantChange.changed_at >= since)
    if field:
        query = query.where(GrantChange.field == field)
    if grant_id:
        query = query.where(GrantChange.grant_id == grant_id)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = session.execute(
        query.order_by(GrantChange.changed_at.desc(), GrantChange.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return Page(
        items=[change_out(change, title) for change, title in rows],
        total=total,
        page=page,
        page_size=page_size,
    )
