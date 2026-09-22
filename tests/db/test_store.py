"""Grant storage: matching, merging, change history, status and review items (SPEC §8)."""

from datetime import date
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fundscout.config import Settings
from fundscout.db.models import (
    Grant,
    GrantChange,
    GrantSource,
    GrantStatus,
    RawDocument,
    ReviewItem,
    ReviewReason,
    RunStatus,
    Source,
    SourceRun,
    SourceType,
)
from fundscout.extract.schema import CandidateGrant
from fundscout.normalise.grant import NormalisationResult, normalise_candidate
from fundscout.pipeline.store import (
    check_repeated_failures,
    refresh_statuses,
    store_grants,
    track_presence,
)
from fundscout.taxonomy.loader import seed_taxonomy

TODAY = date(2026, 9, 21)
SETTINGS = Settings()


@pytest.fixture(autouse=True)
def taxonomy(db_session: Session) -> None:
    seed_taxonomy(db_session)


def make_source(session: Session, name: str, *, primary: bool = False) -> Source:
    source = Source(
        name=name,
        adapter="generic_rss",
        base_url=f"https://{name}.example.org",
        source_type=SourceType.RSS,
        config={"feed_url": f"https://{name}.example.org/feed", "primary_source": primary},
        terms_reviewed=True,
    )
    session.add(source)
    session.flush()
    return source


def make_raw(session: Session, source: Source, url: str) -> RawDocument:
    raw = RawDocument(
        source_id=source.id, url=url, content_hash="0" * 64, storage_path="00/00/test"
    )
    session.add(raw)
    session.flush()
    return raw


def result(**fields: Any) -> NormalisationResult:
    base: dict[str, Any] = {
        "title": "Community Health Innovation Fund",
        "funder_name": "Example Health Foundation",
        "amount_text": "up to USD 50,000",
        "closing_date": "2026-11-30",
        "themes": ["health"],
        "countries": ["Ghana"],
    }
    base.update(fields)
    return normalise_candidate(CandidateGrant.model_validate(base))


def store(
    session: Session, source: Source, url: str, *results: NormalisationResult, **kwargs: Any
) -> Any:
    raw = make_raw(session, source, url)
    return store_grants(
        session, source, raw, list(results), settings=SETTINGS, today=TODAY, **kwargs
    )


def count(session: Session, model: type[Any]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def reviews(session: Session, reason: ReviewReason) -> list[ReviewItem]:
    return list(session.scalars(select(ReviewItem).where(ReviewItem.reason == reason)))


def test_new_grant_is_created_with_everything_linked(db_session: Session) -> None:
    source = make_source(db_session, "funder", primary=True)
    stats = store(db_session, source, "https://funder.example.org/fund", result())
    assert stats.created == 1

    grant = db_session.scalars(select(Grant)).one()
    assert grant.title == "Community Health Innovation Fund"
    assert grant.funder is not None and grant.funder.normalised_name == "example health"
    assert grant.amount_max == 50000 and grant.currency == "USD"
    assert grant.status == GrantStatus.OPEN
    assert [t.slug for t in grant.themes] == ["health"]
    assert [c.country_code for c in grant.countries] == ["GH"]
    assert grant.fingerprint and grant.funder_page_url == "https://funder.example.org/fund"
    [link] = grant.sources
    assert link.is_primary and link.item_key == ""
    [created] = db_session.scalars(select(GrantChange)).all()
    assert created.field == "created"


def test_same_document_twice_updates_nothing(db_session: Session) -> None:
    source = make_source(db_session, "funder", primary=True)
    store(db_session, source, "https://funder.example.org/fund", result())
    stats = store(db_session, source, "https://funder.example.org/fund", result())
    assert (stats.created, stats.updated, stats.unchanged) == (0, 0, 1)
    assert count(db_session, Grant) == 1
    assert count(db_session, GrantSource) == 1
    assert count(db_session, GrantChange) == 1  # just "created"


def test_deadline_change_records_history_and_review(db_session: Session) -> None:
    source = make_source(db_session, "funder", primary=True)
    store(db_session, source, "https://funder.example.org/fund", result())
    stats = store(
        db_session,
        source,
        "https://funder.example.org/fund",
        result(closing_date="2026-12-15", amount_text="up to USD 60,000"),
    )
    assert stats.updated == 1
    grant = db_session.scalars(select(Grant)).one()
    assert grant.closing_date == date(2026, 12, 15)
    changes = {
        c.field: (c.old_value, c.new_value)
        for c in db_session.scalars(select(GrantChange).where(GrantChange.field != "created"))
    }
    assert changes["closing_date"] == ("2026-11-30", "2026-12-15")
    assert changes["amount_max"] == ("50000", "60000")
    [item] = reviews(db_session, ReviewReason.DEADLINE_CHANGED)
    assert item.grant_id == grant.id and item.details["new"] == "2026-12-15"
    assert grant.needs_review


def test_grant_in_two_sources_is_merged_and_primary_wins(db_session: Session) -> None:
    official = make_source(db_session, "funder", primary=True)
    aggregator = make_source(db_session, "aggregator")
    store(db_session, official, "https://funder.example.org/fund", result())
    stats = store(
        db_session,
        aggregator,
        "https://aggregator.example.org/listing/123",
        # Different wording of the same grant, a different deadline and a new field.
        result(
            title="Community Health Innovation Fund 2026",
            funder_name="The Example Health Foundation",
            closing_date="2026-12-01",
            contact_email="grants@example.org",
        ),
    )
    assert stats.created == 0
    grant = db_session.scalars(select(Grant)).one()
    assert {(s.url, s.is_primary) for s in grant.sources} == {
        ("https://funder.example.org/fund", True),
        ("https://aggregator.example.org/listing/123", False),
    }
    assert grant.closing_date == date(2026, 11, 30)  # the primary's value stands
    assert grant.title == "Community Health Innovation Fund"
    assert grant.contact_email == "grants@example.org"  # empty field filled
    assert reviews(db_session, ReviewReason.CONFLICTING_VALUES) == []


def test_conflict_between_non_primary_sources(db_session: Session) -> None:
    first = make_source(db_session, "aggregator_a")
    second = make_source(db_session, "aggregator_b")
    store(db_session, first, "https://a.example.org/1", result())
    store(db_session, second, "https://b.example.org/9", result(closing_date="2026-12-05"))
    grant = db_session.scalars(select(Grant)).one()
    assert grant.closing_date == date(2026, 12, 5)  # most recent kept
    [item] = reviews(db_session, ReviewReason.CONFLICTING_VALUES)
    assert item.details["conflicts"] == [
        {"field": "closing_date", "stored": "2026-11-30", "incoming": "2026-12-05"}
    ]
    assert len(reviews(db_session, ReviewReason.DEADLINE_CHANGED)) == 1


def test_similar_title_is_possible_duplicate(db_session: Session) -> None:
    first = make_source(db_session, "aggregator_a")
    second = make_source(db_session, "aggregator_b")
    store(db_session, first, "https://a.example.org/1", result())
    store(
        db_session,
        second,
        "https://b.example.org/9",
        result(title="Community Health Research Fellowship Fund"),
    )
    assert count(db_session, Grant) == 2
    [item] = reviews(db_session, ReviewReason.POSSIBLE_DUPLICATE)
    assert 75 <= item.details["score"] < 90


def test_next_years_round_is_a_new_grant(db_session: Session) -> None:
    source = make_source(db_session, "aggregator")
    other = make_source(db_session, "aggregator_b")
    store(db_session, source, "https://a.example.org/1", result())
    store(db_session, other, "https://b.example.org/1", result(closing_date="2027-11-30"))
    assert count(db_session, Grant) == 2
    assert reviews(db_session, ReviewReason.POSSIBLE_DUPLICATE) == []


def test_distinct_listings_of_one_source_are_not_merged(db_session: Session) -> None:
    source = make_source(db_session, "funder", primary=True)
    store(db_session, source, "https://funder.example.org/a", result(title="Seed Grants"))
    store(db_session, source, "https://funder.example.org/b", result(title="Seed Grants 2026"))
    assert count(db_session, Grant) == 2


def test_multi_grant_document(db_session: Session) -> None:
    source = make_source(db_session, "funder", primary=True)
    url = "https://funder.example.org/call.pdf"
    topics = [result(title=f"Outbreak research topic {name}") for name in ("A", "B", "C")]
    assert store(db_session, source, url, *topics).created == 3
    keys = {s.item_key for s in db_session.scalars(select(GrantSource))}
    assert keys == {
        "outbreak research topic a",
        "outbreak research topic b",
        "outbreak research topic c",
    }

    # Re-extraction with one title worded differently: still three grants, re-keyed.
    again = [*topics[:2], result(title="Outbreak research topic C (updated)")]
    stats = store(db_session, source, url, *again)
    assert stats.created == 0
    assert count(db_session, Grant) == 3
    keys = {s.item_key for s in db_session.scalars(select(GrantSource))}
    assert "outbreak research topic c updated" in keys and len(keys) == 3
    assert reviews(db_session, ReviewReason.POSSIBLE_DUPLICATE) == []


def test_issue_review_items(db_session: Session) -> None:
    source = make_source(db_session, "funder")
    store(
        db_session,
        source,
        "https://funder.example.org/fund",
        result(amount_text="up to $5,000", field_confidence={"closing_date": 0.3}),
    )
    assert len(reviews(db_session, ReviewReason.AMBIGUOUS_CURRENCY)) == 1
    assert len(reviews(db_session, ReviewReason.LOW_CONFIDENCE)) == 1
    # The same issues again do not pile up while the items are open.
    store(
        db_session,
        source,
        "https://funder.example.org/fund",
        result(amount_text="up to $5,000", field_confidence={"closing_date": 0.3}),
    )
    assert len(reviews(db_session, ReviewReason.LOW_CONFIDENCE)) == 1


def test_missing_title_is_reviewed_not_stored(db_session: Session) -> None:
    source = make_source(db_session, "funder")
    stats = store(db_session, source, "https://funder.example.org/x", result(title=None))
    assert stats.missing_title == 1 and count(db_session, Grant) == 0
    [item] = reviews(db_session, ReviewReason.MISSING_REQUIRED_FIELDS)
    assert item.raw_document_id is not None


def test_not_a_grant_is_skipped(db_session: Session) -> None:
    source = make_source(db_session, "funder")
    stats = store(
        db_session, source, "https://funder.example.org/news", result(is_grant_opportunity=False)
    )
    assert stats.not_grants == 1 and count(db_session, Grant) == 0


def test_manual_override_is_kept(db_session: Session) -> None:
    source = make_source(db_session, "funder", primary=True)
    store(db_session, source, "https://funder.example.org/fund", result())
    grant = db_session.scalars(select(Grant)).one()
    grant.title = "Corrected title"
    grant.manual_overrides = ["title"]
    store(db_session, source, "https://funder.example.org/fund", result())
    assert grant.title == "Corrected title"


def test_status_refresh(db_session: Session) -> None:
    source = make_source(db_session, "funder")
    store(db_session, source, "https://funder.example.org/fund", result())
    grant = db_session.scalars(select(Grant)).one()
    assert grant.status == GrantStatus.OPEN
    assert refresh_statuses(db_session, today=date(2026, 12, 1)) == 1
    db_session.refresh(grant)
    assert grant.status.value == "closed"
    change = db_session.scalars(select(GrantChange).where(GrantChange.field == "status")).one()
    assert (change.old_value, change.new_value) == ("open", "closed")


def test_missing_from_listing_flags_possibly_removed(db_session: Session) -> None:
    source = make_source(db_session, "funder", primary=True)
    store(db_session, source, "https://funder.example.org/fund", result())
    link = db_session.scalars(select(GrantSource)).one()

    for _ in range(2):
        track_presence(db_session, source, set(), settings=SETTINGS)
    assert link.missing_count == 2 and reviews(db_session, ReviewReason.POSSIBLY_REMOVED) == []
    track_presence(db_session, source, set(), settings=SETTINGS)
    track_presence(db_session, source, set(), settings=SETTINGS)
    assert len(reviews(db_session, ReviewReason.POSSIBLY_REMOVED)) == 1  # once, not per run
    grant = db_session.scalars(select(Grant)).one()
    assert grant.status == GrantStatus.OPEN  # never auto-closed

    track_presence(db_session, source, {"https://funder.example.org/fund"}, settings=SETTINGS)
    assert link.missing_count == 0


def test_repeated_failures_open_one_review_item(db_session: Session) -> None:
    source = make_source(db_session, "funder")
    for _ in range(3):
        db_session.add(
            SourceRun(source_id=source.id, status=RunStatus.FAILED, finished_at=func.now())
        )
        db_session.flush()
        check_repeated_failures(db_session, source, settings=SETTINGS)
    [item] = reviews(db_session, ReviewReason.FETCH_FAILED_REPEATEDLY)
    assert item.details["source"] == "funder"
    check_repeated_failures(db_session, source, settings=SETTINGS)
    assert len(reviews(db_session, ReviewReason.FETCH_FAILED_REPEATEDLY)) == 1
