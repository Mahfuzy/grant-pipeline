from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fundscout.db.models import (
    ChangeSource,
    DeadlineType,
    Funder,
    Grant,
    GrantChange,
    GrantCountry,
    GrantSource,
    GrantStatus,
    RawDocument,
    ReviewItem,
    ReviewReason,
    ReviewStatus,
    RunStatus,
    Source,
    SourceRun,
    SourceType,
    Theme,
)


def make_source(session: Session, name: str = "test-source") -> Source:
    source = Source(
        name=name,
        adapter="generic_rss",
        base_url="https://example.org/feed",
        source_type=SourceType.RSS,
    )
    session.add(source)
    session.flush()
    return source


def test_source_defaults(db_session: Session) -> None:
    source = make_source(db_session)
    db_session.refresh(source)
    assert source.id is not None
    assert source.created_at is not None and source.updated_at is not None
    assert source.enabled is True
    assert source.terms_reviewed is False
    assert source.config == {}


def test_full_grant_graph(db_session: Session) -> None:
    source = make_source(db_session)
    run = SourceRun(source=source)
    raw = RawDocument(
        source_id=source.id,
        url="https://example.org/grant/1",
        http_status=200,
        content_hash="a" * 64,
        storage_path="raw/aa/aaaa",
    )
    funder = Funder(name="Example Foundation", normalised_name="example")
    theme = Theme(slug="t-health", label="Health")
    db_session.add_all([run, raw, funder, theme])
    db_session.flush()

    grant = Grant(
        funder=funder,
        title="Health Innovation Fund",
        normalised_title="health innovation fund",
        amount_min=Decimal("10000"),
        amount_max=Decimal("50000.50"),
        currency="USD",
        closing_date=date(2027, 3, 31),
        documents_required=["budget", "cv"],
        themes=[theme],
        countries=[GrantCountry(country_code="GH")],
    )
    db_session.add(grant)
    db_session.flush()
    db_session.add_all(
        [
            GrantSource(grant=grant, source=source, url=raw.url, raw_document_id=raw.id),
            GrantChange(
                grant=grant,
                source_run_id=run.id,
                field="closing_date",
                old_value=None,
                new_value="2027-03-31",
            ),
            ReviewItem(grant_id=grant.id, reason=ReviewReason.DEADLINE_CHANGED),
        ]
    )
    db_session.flush()
    db_session.expire_all()

    loaded = db_session.scalars(select(Grant)).one()
    assert loaded.status == GrantStatus.UNKNOWN
    assert loaded.deadline_type == DeadlineType.UNKNOWN
    assert loaded.amount_max == Decimal("50000.50")
    assert loaded.funder is not None and loaded.funder.name == "Example Foundation"
    assert [t.slug for t in loaded.themes] == ["t-health"]
    assert [c.country_code for c in loaded.countries] == ["GH"]
    assert loaded.sources[0].missing_count == 0
    assert loaded.changes[0].change_source == ChangeSource.PIPELINE
    assert loaded.changes[0].old_value is None
    assert db_session.scalars(select(ReviewItem)).one().status == ReviewStatus.OPEN
    assert db_session.scalars(select(SourceRun)).one().status == RunStatus.RUNNING

    # JSONB null vs SQL NULL: old_value=None is stored as SQL NULL.
    assert db_session.scalar(text("SELECT old_value IS NULL FROM grant_changes")) is True


def test_grant_delete_cascades(db_session: Session) -> None:
    source = make_source(db_session)
    grant = Grant(title="G", normalised_title="g", countries=[GrantCountry(country_code="KE")])
    db_session.add(grant)
    db_session.flush()
    db_session.add(GrantSource(grant_id=grant.id, source_id=source.id, url="https://x"))
    db_session.flush()
    db_session.delete(grant)
    db_session.flush()
    assert db_session.scalar(text("SELECT count(*) FROM grant_sources")) == 0
    assert db_session.scalar(text("SELECT count(*) FROM grant_countries")) == 0


def test_grant_source_unique_per_source_url(db_session: Session) -> None:
    source = make_source(db_session)
    g1 = Grant(title="A", normalised_title="a")
    g2 = Grant(title="B", normalised_title="b")
    db_session.add_all([g1, g2])
    db_session.flush()
    db_session.add(GrantSource(grant_id=g1.id, source_id=source.id, url="https://x/1"))
    db_session.flush()
    db_session.add(GrantSource(grant_id=g2.id, source_id=source.id, url="https://x/1"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_source_name_unique(db_session: Session) -> None:
    make_source(db_session, "dup")
    with pytest.raises(IntegrityError):
        make_source(db_session, "dup")


def test_enum_check_constraint(db_session: Session) -> None:
    grant = Grant(title="A", normalised_title="a")
    db_session.add(grant)
    db_session.flush()
    with pytest.raises(IntegrityError, match="ck_grants_grant_status"):
        db_session.execute(text("UPDATE grants SET status = 'bogus'"))
