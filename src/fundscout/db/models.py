"""SQLAlchemy models (SPEC §5).

Conventions:
- Every table has a UUID primary key plus `created_at` / `updated_at`.
- Enum-like columns are stored as VARCHAR with a CHECK constraint (`native_enum=False`),
  so adding a value later is a simple migration rather than `ALTER TYPE`.
"""

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column, relationship

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampedBase(Base):
    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


def str_enum(enum_cls: type[enum.StrEnum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=40,
        values_callable=lambda e: [m.value for m in e],
    )


# --- Enums ---------------------------------------------------------------------------------


class SourceType(enum.StrEnum):
    HTML = "html"
    RSS = "rss"
    API = "api"
    PDF = "pdf"


class RunStatus(enum.StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class FunderType(enum.StrEnum):
    FOUNDATION = "foundation"
    GOVERNMENT = "government"
    MULTILATERAL = "multilateral"
    CORPORATE = "corporate"
    OTHER = "other"


class DeadlineType(enum.StrEnum):
    FIXED = "fixed"
    ROLLING = "rolling"
    MULTIPLE = "multiple"
    UNKNOWN = "unknown"


class GrantStatus(enum.StrEnum):
    UPCOMING = "upcoming"
    OPEN = "open"
    CLOSED = "closed"
    ROLLING = "rolling"
    UNKNOWN = "unknown"


class ChangeSource(enum.StrEnum):
    PIPELINE = "pipeline"
    MANUAL = "manual"


class ExtractionMethod(enum.StrEnum):
    LLM = "llm"
    RULES = "rules"
    API = "api"


class ReviewReason(enum.StrEnum):
    LOW_CONFIDENCE = "low_confidence"
    MISSING_REQUIRED_FIELDS = "missing_required_fields"
    CONFLICTING_VALUES = "conflicting_values"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    DEADLINE_CHANGED = "deadline_changed"
    POSSIBLY_REMOVED = "possibly_removed"
    EXTRACTION_FAILED = "extraction_failed"
    AMBIGUOUS_CURRENCY = "ambiguous_currency"
    FETCH_FAILED_REPEATEDLY = "fetch_failed_repeatedly"


class ReviewStatus(enum.StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


# --- Sources and collection ----------------------------------------------------------------


class Source(TimestampedBase):
    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(String(200), unique=True)
    adapter: Mapped[str] = mapped_column(String(100))
    base_url: Mapped[str] = mapped_column(Text)
    source_type: Mapped[SourceType] = mapped_column(str_enum(SourceType, "source_type"))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    schedule: Mapped[str | None] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    terms_reviewed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    notes: Mapped[str | None] = mapped_column(Text)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    runs: Mapped[list["SourceRun"]] = relationship(
        back_populates="source", cascade="all, delete-orphan", passive_deletes=True
    )


class SourceRun(TimestampedBase):
    __tablename__ = "source_runs"

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[RunStatus] = mapped_column(
        str_enum(RunStatus, "run_status"),
        default=RunStatus.RUNNING,
        server_default=RunStatus.RUNNING.value,
    )
    items_discovered: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    items_fetched: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    items_unchanged: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    grants_created: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    grants_updated: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    errors: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_log: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )

    source: Mapped[Source] = relationship(back_populates="runs")


class RawDocument(TimestampedBase):
    __tablename__ = "raw_documents"
    __table_args__ = (
        Index("ix_raw_documents_source_url_fetched", "source_id", "url", "fetched_at"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(200))
    # SHA-256 hex of the content in canonical form (see fetch/hashing.py): used to detect
    # "unchanged". It can differ from the hash of the stored bytes, e.g. when volatile
    # JSON keys are excluded. storage_path is addressed by the hash of the raw bytes.
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    storage_path: Mapped[str] = mapped_column(Text)
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)  # raw Last-Modified header value
    # Last time this snapshot's content was verified current (stored, fetched unchanged,
    # or 304). Skipping a fetch because discovery data is unchanged does not update it.
    last_checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Hash of the discovery data (listing entry, feed entry, API search hit) at last check.
    change_key: Mapped[str | None] = mapped_column(String(64))


# --- Funders and grants --------------------------------------------------------------------


class Funder(TimestampedBase):
    __tablename__ = "funders"

    name: Mapped[str] = mapped_column(Text)
    normalised_name: Mapped[str] = mapped_column(Text, index=True)
    website: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(String(2))  # ISO 3166-1 alpha-2
    funder_type: Mapped[FunderType | None] = mapped_column(str_enum(FunderType, "funder_type"))

    grants: Mapped[list["Grant"]] = relationship(back_populates="funder")


class Grant(TimestampedBase):
    __tablename__ = "grants"

    funder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("funders.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(Text)
    normalised_title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)

    amount_min: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    amount_max: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str | None] = mapped_column(String(3))  # ISO 4217
    amount_text: Mapped[str | None] = mapped_column(Text)

    opening_date: Mapped[date | None] = mapped_column(Date)
    closing_date: Mapped[date | None] = mapped_column(Date, index=True)
    deadline_type: Mapped[DeadlineType] = mapped_column(
        str_enum(DeadlineType, "deadline_type"),
        default=DeadlineType.UNKNOWN,
        server_default=DeadlineType.UNKNOWN.value,
    )
    deadline_text: Mapped[str | None] = mapped_column(Text)

    eligibility_text: Mapped[str | None] = mapped_column(Text)
    application_url: Mapped[str | None] = mapped_column(Text)
    funder_page_url: Mapped[str | None] = mapped_column(Text)

    status: Mapped[GrantStatus] = mapped_column(
        str_enum(GrantStatus, "grant_status"),
        default=GrantStatus.UNKNOWN,
        server_default=GrantStatus.UNKNOWN.value,
        index=True,
    )

    # Phase 2 fields
    requirements: Mapped[str | None] = mapped_column(Text)
    documents_required: Mapped[list[str] | None] = mapped_column(JSONB)
    funder_priorities: Mapped[str | None] = mapped_column(Text)
    contact_email: Mapped[str | None] = mapped_column(Text)
    contact_url: Mapped[str | None] = mapped_column(Text)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)

    funder: Mapped[Funder | None] = relationship(back_populates="grants")
    sources: Mapped[list["GrantSource"]] = relationship(
        back_populates="grant", cascade="all, delete-orphan", passive_deletes=True
    )
    themes: Mapped[list["Theme"]] = relationship(secondary="grant_themes")
    regions: Mapped[list["Region"]] = relationship(secondary="grant_regions")
    org_types: Mapped[list["OrgType"]] = relationship(secondary="grant_org_types")
    grant_types: Mapped[list["GrantType"]] = relationship(secondary="grant_grant_types")
    countries: Mapped[list["GrantCountry"]] = relationship(
        back_populates="grant", cascade="all, delete-orphan", passive_deletes=True
    )
    changes: Mapped[list["GrantChange"]] = relationship(
        back_populates="grant", cascade="all, delete-orphan", passive_deletes=True
    )


class GrantSource(TimestampedBase):
    """Where a grant was found. One row per (source, url)."""

    __tablename__ = "grant_sources"
    __table_args__ = (UniqueConstraint("source_id", "url"),)

    grant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("grants.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    raw_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("raw_documents.id", ondelete="SET NULL")
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    missing_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    grant: Mapped[Grant] = relationship(back_populates="sources")
    source: Mapped[Source] = relationship()


# --- Taxonomy ------------------------------------------------------------------------------


class TaxonomyBase(TimestampedBase):
    __abstract__ = True

    slug: Mapped[str] = mapped_column(String(100), unique=True)
    label: Mapped[str] = mapped_column(String(200))

    @declared_attr
    def parent_id(cls) -> Mapped[uuid.UUID | None]:
        return mapped_column(ForeignKey(f"{cls.__tablename__}.id", ondelete="SET NULL"))


class Theme(TaxonomyBase):
    __tablename__ = "themes"


class Region(TaxonomyBase):
    __tablename__ = "regions"

    countries: Mapped[list["RegionCountry"]] = relationship(
        back_populates="region", cascade="all, delete-orphan", passive_deletes=True
    )


class OrgType(TaxonomyBase):
    __tablename__ = "org_types"


class GrantType(TaxonomyBase):
    __tablename__ = "grant_types"


class RegionCountry(TimestampedBase):
    """Countries covered by a region, including those of its sub-regions."""

    __tablename__ = "region_countries"
    __table_args__ = (UniqueConstraint("region_id", "country_code"),)

    region_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("regions.id", ondelete="CASCADE"))
    country_code: Mapped[str] = mapped_column(String(2), index=True)

    region: Mapped[Region] = relationship(back_populates="countries")


# --- Grant <-> taxonomy join tables --------------------------------------------------------


class GrantTheme(TimestampedBase):
    __tablename__ = "grant_themes"
    __table_args__ = (UniqueConstraint("grant_id", "theme_id"),)

    grant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("grants.id", ondelete="CASCADE"))
    theme_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("themes.id", ondelete="CASCADE"), index=True
    )


class GrantRegion(TimestampedBase):
    __tablename__ = "grant_regions"
    __table_args__ = (UniqueConstraint("grant_id", "region_id"),)

    grant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("grants.id", ondelete="CASCADE"))
    region_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("regions.id", ondelete="CASCADE"), index=True
    )


class GrantOrgType(TimestampedBase):
    __tablename__ = "grant_org_types"
    __table_args__ = (UniqueConstraint("grant_id", "org_type_id"),)

    grant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("grants.id", ondelete="CASCADE"))
    org_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("org_types.id", ondelete="CASCADE"), index=True
    )


class GrantGrantType(TimestampedBase):
    __tablename__ = "grant_grant_types"
    __table_args__ = (UniqueConstraint("grant_id", "grant_type_id"),)

    grant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("grants.id", ondelete="CASCADE"))
    grant_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("grant_types.id", ondelete="CASCADE"), index=True
    )


class GrantCountry(TimestampedBase):
    __tablename__ = "grant_countries"
    __table_args__ = (UniqueConstraint("grant_id", "country_code"),)

    grant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("grants.id", ondelete="CASCADE"))
    country_code: Mapped[str] = mapped_column(String(2), index=True)  # ISO 3166-1 alpha-2

    grant: Mapped[Grant] = relationship(back_populates="countries")


# --- History, audit, review ----------------------------------------------------------------


class GrantChange(TimestampedBase):
    __tablename__ = "grant_changes"

    grant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("grants.id", ondelete="CASCADE"), index=True
    )
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_runs.id", ondelete="SET NULL")
    )
    change_source: Mapped[ChangeSource] = mapped_column(
        str_enum(ChangeSource, "change_source"),
        default=ChangeSource.PIPELINE,
        server_default=ChangeSource.PIPELINE.value,
    )
    field: Mapped[str] = mapped_column(String(100))
    old_value: Mapped[Any] = mapped_column(JSONB(none_as_null=True), nullable=True)
    new_value: Mapped[Any] = mapped_column(JSONB(none_as_null=True), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    grant: Mapped[Grant] = relationship(back_populates="changes")


class ExtractionResult(TimestampedBase):
    __tablename__ = "extraction_results"

    raw_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("raw_documents.id", ondelete="CASCADE"), index=True
    )
    method: Mapped[ExtractionMethod] = mapped_column(
        str_enum(ExtractionMethod, "extraction_method")
    )
    model: Mapped[str | None] = mapped_column(String(200))
    output: Mapped[Any] = mapped_column(JSONB, nullable=False)
    field_confidence: Mapped[dict[str, float] | None] = mapped_column(JSONB)


class ReviewItem(TimestampedBase):
    __tablename__ = "review_items"
    __table_args__ = (Index("ix_review_items_status_reason", "status", "reason"),)

    grant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("grants.id", ondelete="CASCADE"), index=True
    )
    raw_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("raw_documents.id", ondelete="SET NULL")
    )
    reason: Mapped[ReviewReason] = mapped_column(str_enum(ReviewReason, "review_reason"))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    status: Mapped[ReviewStatus] = mapped_column(
        str_enum(ReviewStatus, "review_status"),
        default=ReviewStatus.OPEN,
        server_default=ReviewStatus.OPEN.value,
    )
    resolved_by: Mapped[str | None] = mapped_column(String(200))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[str | None] = mapped_column(Text)
