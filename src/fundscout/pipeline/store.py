"""Store extracted grants: match, merge, record changes, compute status, open review
items (SPEC §4 steps 7-10, §8).

Merge policy for an update (§8.2), by who else lists the grant:
- this source is primary, or no other source lists the grant: its values replace the
  stored ones (a value that disappears from the page is cleared);
- another source is primary: only empty fields are filled; the primary's values stand;
- other sources list it and none is primary: non-empty incoming values win (most recent)
  and a disagreement on closing date or amount opens a `conflicting_values` item.
Fields corrected by hand (`manual_overrides`) are never overwritten.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from fundscout.changes.diff import (
    CONFLICT_FIELDS,
    LIST_FIELDS,
    SCALAR_FIELDS,
    TAXONOMY_FIELDS,
    FieldChange,
    diff,
    is_empty,
    same,
    to_json,
)
from fundscout.changes.status import compute_status
from fundscout.config import Settings
from fundscout.db.models import (
    ChangeSource,
    DeadlineType,
    Funder,
    Grant,
    GrantChange,
    GrantCountry,
    GrantSource,
    GrantStatus,
    GrantType,
    OrgType,
    RawDocument,
    Region,
    ReviewReason,
    RunStatus,
    Source,
    SourceRun,
    TaxonomyBase,
    Theme,
)
from fundscout.dedup.matching import MatchQuery, find_match, fingerprint
from fundscout.extract.schema import ExtractedGrant
from fundscout.normalise.funders import normalise_funder_name
from fundscout.normalise.grant import NormalisationResult
from fundscout.normalise.text import normalise_title
from fundscout.review.queue import open_review_item
from fundscout.sources.registry import get_adapter_class

log = structlog.get_logger(__name__)

TAXONOMY_MODELS: dict[str, type[TaxonomyBase]] = {
    "themes": Theme,
    "regions": Region,
    "org_types": OrgType,
    "grant_types": GrantType,
}
# Normalisation issue codes that become review items (SPEC §8.3).
ISSUE_REVIEW_REASONS = {
    "low_confidence": ReviewReason.LOW_CONFIDENCE,
    "missing_required_fields": ReviewReason.MISSING_REQUIRED_FIELDS,
    "ambiguous_currency": ReviewReason.AMBIGUOUS_CURRENCY,
}
ITEM_KEY_MAX_CHARS = 500


@dataclass
class StoreStats:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    not_grants: int = 0
    missing_title: int = 0

    def add(self, other: "StoreStats") -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))


def is_primary_source(source: Source) -> bool:
    config = get_adapter_class(source.adapter).validate_config(source.config)
    return bool(config.primary_source)


def _funder_country(source: Source) -> str | None:
    config = get_adapter_class(source.adapter).validate_config(source.config)
    country: str | None = config.extraction.funder_country
    return country


class TaxonomyRows:
    """slug -> row lookups, loaded once per store call."""

    def __init__(self, session: Session):
        self.rows: dict[str, dict[str, TaxonomyBase]] = {
            kind: {row.slug: row for row in session.scalars(select(model))}
            for kind, model in TAXONOMY_MODELS.items()
        }

    def resolve(self, kind: str, slugs: list[str]) -> list[Any]:
        rows = []
        for slug in slugs:
            row = self.rows[kind].get(slug)
            if row is None:
                log.warning(
                    "taxonomy slug not in database; run `fundscout taxonomy seed`",
                    kind=kind,
                    slug=slug,
                )
                continue
            rows.append(row)
        return rows


def get_or_create_funder(
    session: Session, name: str, website: str | None, country: str | None
) -> Funder:
    normalised = normalise_funder_name(name)
    funder = session.scalars(
        select(Funder).where(Funder.normalised_name == normalised).order_by(Funder.created_at)
    ).first()
    if funder is None:
        funder = Funder(name=name, normalised_name=normalised, website=website, country=country)
        session.add(funder)
        session.flush()
    elif website and not funder.website:
        funder.website = website
    return funder


def current_values(grant: Grant) -> dict[str, Any]:
    values: dict[str, Any] = {
        name: getattr(grant, name) for name in SCALAR_FIELDS if name != "funder"
    }
    values["funder"] = grant.funder.name if grant.funder else None
    values["documents_required"] = list(grant.documents_required or [])
    values["countries"] = [c.country_code for c in grant.countries]
    for kind in TAXONOMY_FIELDS:
        values[kind] = [row.slug for row in getattr(grant, kind)]
    return values


def incoming_values(extracted: ExtractedGrant, funder: Funder | None) -> dict[str, Any]:
    values: dict[str, Any] = {
        name: getattr(extracted, name)
        for name in SCALAR_FIELDS
        if name not in ("funder", "funder_page_url")
    }
    values["funder"] = funder.name if funder else None
    for name in LIST_FIELDS:
        values[name] = list(getattr(extracted, name))
    return values


def apply_values(
    session: Session, grant: Grant, values: dict[str, Any], taxonomy: TaxonomyRows
) -> None:
    for name, value in values.items():
        if name == "funder":
            continue  # set via funder_id by the caller
        if name == "countries":
            existing = {c.country_code: c for c in grant.countries}
            grant.countries = [
                existing.get(code) or GrantCountry(country_code=code) for code in value
            ]
        elif name in TAXONOMY_FIELDS:
            setattr(grant, name, taxonomy.resolve(name, value))
        elif name == "documents_required":
            grant.documents_required = list(value)
        elif name == "deadline_type":
            grant.deadline_type = DeadlineType(value)
        elif name == "title":
            grant.title = value
            grant.normalised_title = normalise_title(value)
        else:
            setattr(grant, name, value)


def record_changes(
    session: Session,
    grant: Grant,
    changes: Sequence[FieldChange],
    run_id: uuid.UUID | None,
    now: datetime,
    change_source: ChangeSource = ChangeSource.PIPELINE,
    changed_by: str | None = None,
) -> None:
    for change in changes:
        session.add(
            GrantChange(
                grant_id=grant.id,
                source_run_id=run_id,
                change_source=change_source,
                changed_by=changed_by,
                field=change.field,
                old_value=change.old,
                new_value=change.new,
                changed_at=now,
            )
        )


def update_status(
    session: Session,
    grant: Grant,
    today: date,
    run_id: uuid.UUID | None,
    now: datetime,
    change_source: ChangeSource = ChangeSource.PIPELINE,
    changed_by: str | None = None,
) -> bool:
    status = compute_status(grant.opening_date, grant.closing_date, grant.deadline_type, today)
    if status == grant.status:
        return False
    record_changes(
        session,
        grant,
        [FieldChange("status", grant.status.value, status.value)],
        run_id,
        now,
        change_source,
        changed_by,
    )
    grant.status = status
    return True


def _item_keys(results: Sequence[NormalisationResult]) -> list[str]:
    storable = [r for r in results if r.grant.is_grant_opportunity and r.grant.title]
    if len(storable) <= 1:
        return ["" for _ in results]
    return [
        normalise_title(r.grant.title)[:ITEM_KEY_MAX_CHARS] if r.grant.title else ""
        for r in results
    ]


def store_grants(
    session: Session,
    source: Source,
    raw: RawDocument,
    results: Sequence[NormalisationResult],
    *,
    settings: Settings,
    run_id: uuid.UUID | None = None,
    today: date | None = None,
) -> StoreStats:
    """Upsert every grant extracted from one document. Returns counts."""
    today = today or date.today()
    now = datetime.now(UTC)
    stats = StoreStats()
    taxonomy = TaxonomyRows(session)
    primary = is_primary_source(source)
    funder_country = _funder_country(source)
    keys = _item_keys(results)

    # Grants linked to other items of this document are not candidates for this one.
    batch_links = {
        link.item_key: link.grant_id
        for link in session.scalars(
            select(GrantSource).where(
                GrantSource.source_id == source.id, GrantSource.url == raw.url
            )
        )
        if link.item_key in keys
    }
    claimed: set[uuid.UUID] = set()

    for result, item_key in zip(results, keys, strict=True):
        extracted = result.grant
        if not extracted.is_grant_opportunity:
            stats.not_grants += 1
            continue
        if not extracted.title:
            stats.missing_title += 1
            open_review_item(
                session,
                ReviewReason.MISSING_REQUIRED_FIELDS,
                raw_document_id=raw.id,
                details={
                    "url": raw.url,
                    "missing": "title",
                    "extracted": extracted.model_dump(mode="json"),
                },
            )
            continue

        funder = (
            get_or_create_funder(
                session, extracted.funder_name, extracted.funder_website, funder_country
            )
            if extracted.funder_name
            else None
        )
        title_key = normalise_title(extracted.title)
        fp = fingerprint(
            funder.normalised_name if funder else None, title_key, extracted.closing_date
        )
        exclude = claimed | {gid for key, gid in batch_links.items() if key != item_key}
        match = find_match(
            session,
            MatchQuery(
                source_id=source.id,
                url=raw.url,
                item_key=item_key,
                funder_id=funder.id if funder else None,
                normalised_title=title_key,
                closing_date=extracted.closing_date,
                fingerprint=fp,
                exclude=exclude,
            ),
            match_threshold=settings.dedup_match_threshold,
            possible_threshold=settings.dedup_possible_duplicate_threshold,
            date_tolerance_days=settings.dedup_date_tolerance_days,
        )

        with structlog.contextvars.bound_contextvars(item_key=item_key or None):
            if match.grant is None:
                grant = _create(
                    session,
                    source,
                    raw,
                    extracted,
                    funder,
                    item_key,
                    primary,
                    taxonomy,
                    run_id,
                    today,
                    now,
                )
                stats.created += 1
                for other, score in match.possible_duplicates:
                    open_review_item(
                        session,
                        ReviewReason.POSSIBLE_DUPLICATE,
                        grant=grant,
                        details={
                            "other_grant_id": str(other.id),
                            "other_title": other.title,
                            "score": round(score, 1),
                        },
                        dedupe_on={"other_grant_id": str(other.id)},
                    )
            else:
                grant = match.grant
                changed = _update(
                    session,
                    source,
                    raw,
                    extracted,
                    funder,
                    grant,
                    item_key,
                    primary,
                    taxonomy,
                    match.link,
                    run_id,
                    today,
                    now,
                )
                if changed:
                    stats.updated += 1
                else:
                    stats.unchanged += 1
                log.info(
                    "matched grant",
                    method=match.method,
                    score=match.score,
                    grant_id=str(grant.id),
                    changed=changed,
                )
            claimed.add(grant.id)

            for issue_code, reason in ISSUE_REVIEW_REASONS.items():
                issues = [i.as_dict() for i in result.issues if i.code == issue_code]
                if issues:
                    open_review_item(
                        session,
                        reason,
                        grant=grant,
                        raw_document_id=raw.id,
                        details={"url": raw.url, "issues": issues},
                    )
    session.flush()
    return stats


def _create(
    session: Session,
    source: Source,
    raw: RawDocument,
    extracted: ExtractedGrant,
    funder: Funder | None,
    item_key: str,
    primary: bool,
    taxonomy: TaxonomyRows,
    run_id: uuid.UUID | None,
    today: date,
    now: datetime,
) -> Grant:
    assert extracted.title
    grant = Grant(
        funder_id=funder.id if funder else None,
        title=extracted.title,
        normalised_title=normalise_title(extracted.title),
        first_seen_at=now,
        last_seen_at=now,
        last_checked_at=now,
        funder_page_url=raw.url if primary else None,
        manual_overrides=[],
    )
    session.add(grant)
    apply_values(session, grant, incoming_values(extracted, funder), taxonomy)
    grant.status = compute_status(
        grant.opening_date, grant.closing_date, grant.deadline_type, today
    )
    grant.fingerprint = grant_fingerprint(grant, funder)
    grant.sources.append(
        GrantSource(
            source_id=source.id,
            url=raw.url,
            item_key=item_key,
            raw_document_id=raw.id,
            is_primary=primary,
            first_seen_at=now,
            last_seen_at=now,
        )
    )
    session.flush()
    record_changes(
        session,
        grant,
        [FieldChange("created", None, {"title": grant.title, "source": source.name})],
        run_id,
        now,
    )
    log.info("created grant", grant_id=str(grant.id), title=grant.title[:120])
    return grant


def grant_fingerprint(grant: Grant, funder: Funder | None) -> str:
    return fingerprint(
        funder.normalised_name if funder else None, grant.normalised_title, grant.closing_date
    )


def _update(
    session: Session,
    source: Source,
    raw: RawDocument,
    extracted: ExtractedGrant,
    funder: Funder | None,
    grant: Grant,
    item_key: str,
    primary: bool,
    taxonomy: TaxonomyRows,
    link: GrantSource | None,
    run_id: uuid.UUID | None,
    today: date,
    now: datetime,
) -> bool:
    """Merge an extraction into an existing grant. Returns whether anything changed."""
    if link is None:
        # Same document under an older item key (titles in multi-grant documents can
        # shift between extractions), or this source is new for the grant.
        link = next(
            (gs for gs in grant.sources if gs.source_id == source.id and gs.url == raw.url), None
        )
        if link is not None:
            link.item_key = item_key
        else:
            link = GrantSource(
                source_id=source.id, url=raw.url, item_key=item_key, first_seen_at=now
            )
            grant.sources.append(link)
            log.info("grant found in another source", grant_id=str(grant.id))
    link.raw_document_id = raw.id
    link.last_seen_at = now
    link.missing_count = 0
    link.is_primary = primary

    others = [gs for gs in grant.sources if gs.source_id != source.id]
    if primary or not others:
        mode = "replace"
    elif any(gs.is_primary for gs in others):
        mode = "fill"
    else:
        mode = "latest"

    old = current_values(grant)
    incoming = incoming_values(extracted, funder)
    if primary:
        incoming["funder_page_url"] = raw.url
    proposed: dict[str, Any] = {}
    conflicts: list[dict[str, Any]] = []
    for name, value in incoming.items():
        if name in (grant.manual_overrides or []):
            continue
        if mode == "replace":
            proposed[name] = value
        elif mode == "fill":
            if is_empty(old.get(name)) and not is_empty(value):
                proposed[name] = value
        elif not is_empty(value):
            if (
                name in CONFLICT_FIELDS
                and not is_empty(old.get(name))
                and not same(old[name], value)
            ):
                conflicts.append(
                    {"field": name, "stored": to_json(old[name]), "incoming": to_json(value)}
                )
            proposed[name] = value

    changes = diff(old, proposed)
    changed_names = {c.field for c in changes}
    if "funder" in changed_names:
        grant.funder_id = funder.id if funder else None
        session.flush()
        session.refresh(grant, ["funder"])
    apply_values(
        session, grant, {k: v for k, v in proposed.items() if k in changed_names}, taxonomy
    )
    grant.fingerprint = grant_fingerprint(grant, grant.funder)
    grant.last_seen_at = now
    grant.last_checked_at = now
    record_changes(session, grant, changes, run_id, now)
    status_changed = update_status(session, grant, today, run_id, now)

    for change in changes:
        if change.field == "closing_date" and change.old is not None:
            open_review_item(
                session,
                ReviewReason.DEADLINE_CHANGED,
                grant=grant,
                raw_document_id=raw.id,
                details={
                    "old": change.old,
                    "new": change.new,
                    "source": source.name,
                    "url": raw.url,
                },
                dedupe=False,
            )
    if conflicts:
        open_review_item(
            session,
            ReviewReason.CONFLICTING_VALUES,
            grant=grant,
            raw_document_id=raw.id,
            details={
                "source": source.name,
                "url": raw.url,
                "conflicts": conflicts,
                "kept": "incoming (most recent)",
            },
            dedupe_on={"source": source.name},
        )
    if changes:
        log.info("updated grant", grant_id=str(grant.id), fields=sorted(changed_names))
    return bool(changes) or status_changed


def refresh_statuses(session: Session, *, today: date | None = None) -> int:
    """Recompute status for grants whose status can change with time alone (dates pass)."""
    today = today or date.today()
    now = datetime.now(UTC)
    changed = 0
    for grant in session.scalars(select(Grant).where(Grant.status != GrantStatus.CLOSED)):
        if update_status(session, grant, today, None, now):
            changed += 1
    session.flush()
    log.info("statuses refreshed", changed=changed)
    return changed


def track_presence(
    session: Session,
    source: Source,
    seen_urls: set[str],
    *,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    """After a complete discovery: reset missing counts for listed documents, increment
    them for the rest, and flag `possibly_removed` (never auto-close) when a grant has
    been missing from its primary listing for `missing_runs_before_review` runs."""
    now = now or datetime.now(UTC)
    links = session.scalars(select(GrantSource).where(GrantSource.source_id == source.id)).all()
    for link in links:
        grant = link.grant
        if link.url in seen_urls:
            link.missing_count = 0
            link.last_seen_at = now
            grant.last_seen_at = now
            continue
        link.missing_count += 1
        governs = link.is_primary or not any(gs.is_primary for gs in grant.sources)
        if (
            governs
            and link.missing_count >= settings.missing_runs_before_review
            and grant.status != GrantStatus.CLOSED
        ):
            open_review_item(
                session,
                ReviewReason.POSSIBLY_REMOVED,
                grant=grant,
                details={
                    "source": source.name,
                    "url": link.url,
                    "missing_count": link.missing_count,
                },
                dedupe_on={"url": link.url},
            )
    session.flush()


def check_repeated_failures(session: Session, source: Source, *, settings: Settings) -> bool:
    """Open a `fetch_failed_repeatedly` item (and log an alert) when the source's last
    `fetch_failures_before_review` runs all failed. Returns whether the source is failing."""
    threshold = settings.fetch_failures_before_review
    recent = session.scalars(
        select(SourceRun.status)
        .where(SourceRun.source_id == source.id, SourceRun.finished_at.is_not(None))
        .order_by(SourceRun.started_at.desc())
        .limit(threshold)
    ).all()
    if len(recent) < threshold or any(status != RunStatus.FAILED for status in recent):
        return False
    log.error(
        "ALERT: source failing repeatedly", source=source.name, consecutive_failures=threshold
    )
    open_review_item(
        session,
        ReviewReason.FETCH_FAILED_REPEATEDLY,
        details={
            "source": source.name,
            "source_id": str(source.id),
            "consecutive_failures": threshold,
        },
        dedupe_on={"source_id": str(source.id)},
    )
    return True
