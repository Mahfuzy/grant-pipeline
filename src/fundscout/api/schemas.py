"""API request/response models."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Page[T](BaseModel):
    items: list[T]
    total: int
    page: int
    page_size: int


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class FunderOut(ORM):
    id: uuid.UUID
    name: str
    website: str | None
    country: str | None


class GrantSummary(ORM):
    id: uuid.UUID
    title: str
    funder: FunderOut | None
    amount_min: Decimal | None
    amount_max: Decimal | None
    currency: str | None
    opening_date: date | None
    closing_date: date | None
    deadline_type: str
    status: str
    needs_review: bool
    application_url: str | None
    themes: list[str] = []
    regions: list[str] = []
    countries: list[str] = []
    first_seen_at: datetime
    last_seen_at: datetime


class GrantSourceOut(ORM):
    source_id: uuid.UUID
    source_name: str
    url: str
    item_key: str
    is_primary: bool
    first_seen_at: datetime
    last_seen_at: datetime
    missing_count: int
    raw_document_id: uuid.UUID | None


class ChangeOut(ORM):
    id: uuid.UUID
    grant_id: uuid.UUID
    grant_title: str | None = None
    source_run_id: uuid.UUID | None
    change_source: str
    changed_by: str | None = None
    field: str
    old_value: Any
    new_value: Any
    changed_at: datetime


class GrantDetail(GrantSummary):
    description: str | None
    amount_text: str | None
    deadline_text: str | None
    eligibility_text: str | None
    funder_page_url: str | None
    requirements: str | None
    documents_required: list[str] | None
    funder_priorities: str | None
    contact_email: str | None
    contact_url: str | None
    org_types: list[str] = []
    grant_types: list[str] = []
    manual_overrides: list[str] = []
    fingerprint: str | None
    last_checked_at: datetime
    sources: list[GrantSourceOut] = []
    changes: list[ChangeOut] = []
    review_items: list["ReviewItemOut"] = []


class GrantPatch(BaseModel):
    """Manual corrections. Only fields present in the body are changed (null clears)."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1)
    description: str | None = None
    amount_min: Decimal | None = Field(default=None, ge=0)
    amount_max: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    amount_text: str | None = None
    opening_date: date | None = None
    closing_date: date | None = None
    deadline_type: Literal["fixed", "rolling", "multiple", "unknown"] | None = None
    deadline_text: str | None = None
    eligibility_text: str | None = None
    application_url: str | None = None
    requirements: str | None = None
    funder_priorities: str | None = None
    contact_email: str | None = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    contact_url: str | None = None
    countries: list[str] | None = None
    regions: list[str] | None = None
    org_types: list[str] | None = None
    themes: list[str] | None = None
    grant_types: list[str] | None = None
    changed_by: str | None = Field(default=None, description="Who made the correction")
    # Hand these fields back to the pipeline (removes them from manual_overrides).
    release_overrides: list[str] | None = None


class SourceOut(ORM):
    id: uuid.UUID
    name: str
    adapter: str
    base_url: str
    source_type: str
    schedule: str | None
    enabled: bool
    terms_reviewed: bool
    notes: str | None
    last_run_at: datetime | None
    last_success_at: datetime | None
    last_run: "RunOut | None" = None
    grant_count: int = 0


class RunOut(ORM):
    id: uuid.UUID
    source_id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None
    status: str
    items_discovered: int
    items_fetched: int
    items_unchanged: int
    grants_created: int
    grants_updated: int
    errors: int
    error_log: list[dict[str, Any]]


class RunRequested(BaseModel):
    source: str
    message: str


class ReviewItemOut(ORM):
    id: uuid.UUID
    grant_id: uuid.UUID | None
    grant_title: str | None = None
    raw_document_id: uuid.UUID | None
    reason: str
    details: dict[str, Any]
    status: str
    resolved_by: str | None
    resolved_at: datetime | None
    resolution_note: str | None
    created_at: datetime


class RawDocumentOut(ORM):
    id: uuid.UUID
    source_name: str
    url: str
    fetched_at: datetime
    content_type: str | None
    http_status: int | None


class ExtractionOut(ORM):
    id: uuid.UUID
    method: str
    model: str | None
    output: Any
    created_at: datetime


class ReviewItemDetail(ReviewItemOut):
    grant: GrantDetail | None = None
    raw_document: RawDocumentOut | None = None
    extraction: ExtractionOut | None = None
    related_grant: GrantSummary | None = None


class ReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolved_by: str | None = None
    note: str | None = None


GrantDetail.model_rebuild()
SourceOut.model_rebuild()
