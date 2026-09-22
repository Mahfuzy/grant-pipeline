"""ORM -> API model conversion."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from fundscout.api.schemas import (
    ChangeOut,
    ExtractionOut,
    FunderOut,
    GrantDetail,
    GrantSourceOut,
    GrantSummary,
    RawDocumentOut,
    ReviewItemDetail,
    ReviewItemOut,
    RunOut,
    SourceOut,
)
from fundscout.db.models import (
    ExtractionResult,
    Grant,
    GrantChange,
    RawDocument,
    ReviewItem,
    Source,
    SourceRun,
)


def grant_summary(grant: Grant) -> GrantSummary:
    return GrantSummary(
        id=grant.id,
        title=grant.title,
        funder=FunderOut.model_validate(grant.funder) if grant.funder else None,
        amount_min=grant.amount_min,
        amount_max=grant.amount_max,
        currency=grant.currency,
        opening_date=grant.opening_date,
        closing_date=grant.closing_date,
        deadline_type=grant.deadline_type.value,
        status=grant.status.value,
        needs_review=grant.needs_review,
        application_url=grant.application_url,
        themes=[t.slug for t in grant.themes],
        regions=[r.slug for r in grant.regions],
        countries=sorted(c.country_code for c in grant.countries),
        first_seen_at=grant.first_seen_at,
        last_seen_at=grant.last_seen_at,
    )


def change_out(change: GrantChange, title: str | None = None) -> ChangeOut:
    return ChangeOut(
        id=change.id,
        grant_id=change.grant_id,
        grant_title=title,
        source_run_id=change.source_run_id,
        change_source=change.change_source.value,
        changed_by=change.changed_by,
        field=change.field,
        old_value=change.old_value,
        new_value=change.new_value,
        changed_at=change.changed_at,
    )


def review_out(item: ReviewItem, grant_title: str | None = None) -> ReviewItemOut:
    return ReviewItemOut(
        id=item.id,
        grant_id=item.grant_id,
        grant_title=grant_title,
        raw_document_id=item.raw_document_id,
        reason=item.reason.value,
        details=item.details,
        status=item.status.value,
        resolved_by=item.resolved_by,
        resolved_at=item.resolved_at,
        resolution_note=item.resolution_note,
        created_at=item.created_at,
    )


def grant_detail(session: Session, grant: Grant) -> GrantDetail:
    summary = grant_summary(grant).model_dump()
    changes = session.scalars(
        select(GrantChange)
        .where(GrantChange.grant_id == grant.id)
        .order_by(GrantChange.changed_at.desc(), GrantChange.field)
    ).all()
    reviews = session.scalars(
        select(ReviewItem)
        .where(ReviewItem.grant_id == grant.id)
        .order_by(ReviewItem.created_at.desc())
    ).all()
    return GrantDetail(
        **summary,
        description=grant.description,
        amount_text=grant.amount_text,
        deadline_text=grant.deadline_text,
        eligibility_text=grant.eligibility_text,
        funder_page_url=grant.funder_page_url,
        requirements=grant.requirements,
        documents_required=grant.documents_required,
        funder_priorities=grant.funder_priorities,
        contact_email=grant.contact_email,
        contact_url=grant.contact_url,
        org_types=[o.slug for o in grant.org_types],
        grant_types=[g.slug for g in grant.grant_types],
        manual_overrides=list(grant.manual_overrides or []),
        fingerprint=grant.fingerprint,
        last_checked_at=grant.last_checked_at,
        sources=[
            GrantSourceOut(
                source_id=gs.source_id,
                source_name=gs.source.name,
                url=gs.url,
                item_key=gs.item_key,
                is_primary=gs.is_primary,
                first_seen_at=gs.first_seen_at,
                last_seen_at=gs.last_seen_at,
                missing_count=gs.missing_count,
                raw_document_id=gs.raw_document_id,
            )
            for gs in sorted(grant.sources, key=lambda gs: (not gs.is_primary, gs.first_seen_at))
        ],
        changes=[change_out(c) for c in changes],
        review_items=[review_out(r, grant.title) for r in reviews],
    )


def source_out(session: Session, source: Source, grant_count: int = 0) -> SourceOut:
    last = session.scalars(
        select(SourceRun)
        .where(SourceRun.source_id == source.id)
        .order_by(SourceRun.started_at.desc())
        .limit(1)
    ).first()
    return SourceOut(
        id=source.id,
        name=source.name,
        adapter=source.adapter,
        base_url=source.base_url,
        source_type=source.source_type.value,
        schedule=source.schedule,
        enabled=source.enabled,
        terms_reviewed=source.terms_reviewed,
        notes=source.notes,
        last_run_at=source.last_run_at,
        last_success_at=source.last_success_at,
        last_run=run_out(last) if last else None,
        grant_count=grant_count,
    )


def run_out(run: SourceRun) -> RunOut:
    return RunOut(
        id=run.id,
        source_id=run.source_id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        status=run.status.value,
        items_discovered=run.items_discovered,
        items_fetched=run.items_fetched,
        items_unchanged=run.items_unchanged,
        grants_created=run.grants_created,
        grants_updated=run.grants_updated,
        errors=run.errors,
        error_log=run.error_log,
    )


def review_detail(session: Session, item: ReviewItem) -> ReviewItemDetail:
    grant = session.get(Grant, item.grant_id) if item.grant_id else None
    raw_id = item.raw_document_id
    if raw_id is None and grant is not None:
        primary = sorted(grant.sources, key=lambda gs: (not gs.is_primary, gs.first_seen_at))
        raw_id = next((gs.raw_document_id for gs in primary if gs.raw_document_id), None)
    raw = session.get(RawDocument, raw_id) if raw_id else None
    extraction = (
        session.scalars(
            select(ExtractionResult)
            .where(ExtractionResult.raw_document_id == raw.id)
            .order_by(ExtractionResult.created_at.desc())
            .limit(1)
        ).first()
        if raw
        else None
    )
    related_id = item.details.get("other_grant_id") if item.details else None
    related = session.get(Grant, related_id) if related_id else None
    raw_source = session.get(Source, raw.source_id) if raw else None
    return ReviewItemDetail(
        **review_out(item, grant.title if grant else None).model_dump(),
        grant=grant_detail(session, grant) if grant else None,
        raw_document=RawDocumentOut(
            id=raw.id,
            source_name=raw_source.name if raw_source else "",
            url=raw.url,
            fetched_at=raw.fetched_at,
            content_type=raw.content_type,
            http_status=raw.http_status,
        )
        if raw
        else None,
        extraction=ExtractionOut(
            id=extraction.id,
            method=extraction.method.value,
            model=extraction.model,
            output=extraction.output,
            created_at=extraction.created_at,
        )
        if extraction
        else None,
        related_grant=grant_summary(related) if related else None,
    )
