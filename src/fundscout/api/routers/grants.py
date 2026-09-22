"""GET /grants, GET /grants/{id}, PATCH /grants/{id} (SPEC §11)."""

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, literal_column, or_, select
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import Select

from fundscout.api.deps import SessionDep
from fundscout.api.schemas import GrantDetail, GrantPatch, GrantSummary, Page
from fundscout.api.serialise import grant_detail, grant_summary
from fundscout.changes.diff import TAXONOMY_FIELDS, diff
from fundscout.db.models import (
    ChangeSource,
    Funder,
    Grant,
    GrantCountry,
    GrantStatus,
    OrgType,
    Region,
    RegionCountry,
    Theme,
)
from fundscout.normalise.countries import to_alpha2
from fundscout.pipeline.store import (
    TaxonomyRows,
    apply_values,
    current_values,
    grant_fingerprint,
    record_changes,
    update_status,
)

router = APIRouter(prefix="/grants", tags=["grants"])

SORTS: dict[str, tuple[Any, ...]] = {
    "closing_date": (Grant.closing_date.asc().nulls_last(), Grant.title),
    "-closing_date": (Grant.closing_date.desc().nulls_last(), Grant.title),
    "-first_seen_at": (Grant.first_seen_at.desc(), Grant.title),
    "title": (Grant.title,),
}


def search_vector() -> Any:
    # Same expression as the ix_grants_search index.
    return func.to_tsvector(
        literal_column("'english'::regconfig"),
        func.coalesce(Grant.title, "") + " " + func.coalesce(Grant.description, ""),
    )


def filter_grants(
    query: Select[Any],
    *,
    status_: list[GrantStatus] | None,
    theme: str | None,
    region: str | None,
    country: str | None,
    org_type: str | None,
    closing_before: date | None,
    closing_after: date | None,
    q: str | None,
    needs_review: bool | None,
    funder: str | None,
) -> Select[Any]:
    if status_:
        query = query.where(Grant.status.in_(status_))
    if theme:
        query = query.where(Grant.themes.any(Theme.slug == theme))
    if org_type:
        query = query.where(Grant.org_types.any(OrgType.slug == org_type))
    if region:
        # Tagged with the region, or with a country inside it.
        region_countries = (
            select(RegionCountry.country_code).join(Region).where(Region.slug == region)
        )
        query = query.where(
            or_(
                Grant.regions.any(Region.slug == region),
                Grant.countries.any(GrantCountry.country_code.in_(region_countries)),
            )
        )
    if country:
        code = country.upper()
        # Tagged with the country, or with a region that contains it.
        query = query.where(
            or_(
                Grant.countries.any(GrantCountry.country_code == code),
                Grant.regions.any(Region.countries.any(RegionCountry.country_code == code)),
            )
        )
    if closing_before:
        query = query.where(Grant.closing_date <= closing_before)
    if closing_after:
        query = query.where(Grant.closing_date >= closing_after)
    if needs_review is not None:
        query = query.where(Grant.needs_review.is_(needs_review))
    if funder:
        query = query.where(Grant.funder.has(Funder.name.ilike(f"%{funder}%")))
    if q and q.strip():
        query = query.where(
            or_(
                search_vector().op("@@")(func.websearch_to_tsquery("english", q)),
                Grant.title.ilike(f"%{q.strip()}%"),
            )
        )
    return query


@router.get("", response_model=Page[GrantSummary])
def list_grants(
    session: SessionDep,
    status_: Annotated[list[GrantStatus] | None, Query(alias="status")] = None,
    theme: str | None = None,
    region: str | None = None,
    country: str | None = Query(None, min_length=2, max_length=2),
    org_type: str | None = None,
    closing_before: date | None = None,
    closing_after: date | None = None,
    q: str | None = Query(None, max_length=200, description="Text search"),
    needs_review: bool | None = None,
    funder: str | None = None,
    sort: Literal["closing_date", "-closing_date", "-first_seen_at", "title"] = "closing_date",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> Page[GrantSummary]:
    query = filter_grants(
        select(Grant),
        status_=status_,
        theme=theme,
        region=region,
        country=country,
        org_type=org_type,
        closing_before=closing_before,
        closing_after=closing_after,
        q=q,
        needs_review=needs_review,
        funder=funder,
    )
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    grants = session.scalars(
        query.options(
            selectinload(Grant.funder),
            selectinload(Grant.themes),
            selectinload(Grant.regions),
            selectinload(Grant.countries),
        )
        .order_by(*SORTS[sort], Grant.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return Page(
        items=[grant_summary(g) for g in grants], total=total, page=page, page_size=page_size
    )


def _get(session: SessionDep, grant_id: uuid.UUID) -> Grant:
    grant = session.get(Grant, grant_id)
    if grant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "grant not found")
    return grant


@router.get("/{grant_id}", response_model=GrantDetail)
def get_grant(grant_id: uuid.UUID, session: SessionDep) -> GrantDetail:
    return grant_detail(session, _get(session, grant_id))


@router.patch("/{grant_id}", response_model=GrantDetail)
def patch_grant(grant_id: uuid.UUID, patch: GrantPatch, session: SessionDep) -> GrantDetail:
    """Manual correction. Changed fields are recorded in grant_changes (source "manual")
    and protected from later pipeline updates until released."""
    grant = _get(session, grant_id)
    fields = patch.model_fields_set - {"changed_by", "release_overrides"}
    taxonomy = TaxonomyRows(session)
    values: dict[str, Any] = {}
    errors: list[str] = []
    for name in fields:
        value = getattr(patch, name)
        if name in TAXONOMY_FIELDS:
            value = value or []
            unknown = [v for v in value if v not in taxonomy.rows[name]]
            if unknown:
                errors.append(f"{name}: unknown slugs {unknown}")
        elif name == "countries":
            codes = [to_alpha2(v) for v in value or []]
            if None in codes:
                unknown = [v for v, c in zip(value, codes, strict=True) if c is None]
                errors.append(f"countries: unknown {unknown}")
            value = [c for c in codes if c]
        elif name == "title" and value is None:
            errors.append("title cannot be cleared")
        elif name == "deadline_type" and value is None:
            value = "unknown"
        values[name] = value
    if errors:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, errors)
    amount_min = values.get("amount_min", grant.amount_min)
    amount_max = values.get("amount_max", grant.amount_max)
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "amount_min > amount_max")

    now = datetime.now(UTC)
    changes = diff(current_values(grant), values)
    changed = {c.field for c in changes}
    apply_values(session, grant, {k: v for k, v in values.items() if k in changed}, taxonomy)
    overrides = set(grant.manual_overrides or []) | changed
    overrides -= set(patch.release_overrides or [])
    grant.manual_overrides = sorted(overrides)
    grant.fingerprint = grant_fingerprint(grant, grant.funder)
    record_changes(session, grant, changes, None, now, ChangeSource.MANUAL, patch.changed_by)
    update_status(session, grant, date.today(), None, now, ChangeSource.MANUAL, patch.changed_by)
    session.commit()
    session.refresh(grant)
    return grant_detail(session, grant)
