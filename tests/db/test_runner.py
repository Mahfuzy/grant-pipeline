import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fundscout.config import Settings
from fundscout.db.models import RawDocument, RunStatus, Source, SourceRun
from fundscout.fetch.http import HttpClient
from fundscout.fetch.ratelimit import DomainRateLimiter
from fundscout.fetch.storage import LocalRawStorage
from fundscout.pipeline.runner import SourceNotRunnable, build_http_client, run_source
from fundscout.sources.spec import load_source_spec, upsert_source
from tests.fakeweb import FakeWeb, fixture_bytes, json_body, no_sleep

ROOT = Path(__file__).parent.parent.parent
EDCTP = "https://www.global-health-edctp3.europa.eu"
LISTING = f"{EDCTP}/funding/calls-proposals_en"
FT_JSON = "https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/"
FT_LAST_MODIFIED = "Fri, 04 Sep 2026 13:48:20 GMT"
SEARCH = "https://api.grants.gov/v1/api/search2"
FETCH = "https://api.grants.gov/v1/api/fetchOpportunity"


def add_source(session: Session, name: str, **overrides: Any) -> Source:
    spec = load_source_spec(ROOT / "sources" / f"{name}.yaml")
    spec = spec.model_copy(update=overrides)
    source, _ = upsert_source(session, spec)
    session.commit()
    return source


def http_client(web: FakeWeb) -> HttpClient:
    return build_http_client(
        Settings(),
        web.transport(),
        sleep=no_sleep,
        rate_limiter=DomainRateLimiter(sleep=no_sleep),
    )


async def run(
    session: Session, name: str, web: FakeWeb, tmp_path: Path, **kwargs: Any
) -> SourceRun:
    async with http_client(web) as http:
        return await run_source(
            session,
            name,
            settings=Settings(),
            http=http,
            storage=LocalRawStorage(tmp_path / "raw"),
            **kwargs,
        )


def snapshot_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(RawDocument)) or 0


# --- Grants.gov ---------------------------------------------------------------------------


class GrantsGovWeb(FakeWeb):
    """Search returns three real hits (two posted, one forecast). Every detail response
    gets a fresh token, like the real API; 355824 alternates between two real fetches
    whose lists come back in different orders."""

    def __init__(self) -> None:
        super().__init__()
        posted = json.loads(fixture_bytes("grants_gov/search2_posted_rows5_start0.json"))
        forecast = json.loads(fixture_bytes("grants_gov/search2_forecast_rows3.json"))
        hits = [h for h in posted["data"]["oppHits"] if h["id"] in ("357305", "357658")]
        hits += [h for h in forecast["data"]["oppHits"] if h["id"] == "355824"]
        self.search_data = posted
        self.search_data["data"]["oppHits"] = hits
        self.search_data["data"]["hitCount"] = len(hits)
        self.details: dict[str, list[dict[str, Any]]] = {
            "357305": [json.loads(fixture_bytes("grants_gov/fetch_357305.json"))],
            "357658": [json.loads(fixture_bytes("grants_gov/fetch_357658.json"))],
            "355824": [
                json.loads(fixture_bytes("grants_gov/fetch_355824_a.json")),
                json.loads(fixture_bytes("grants_gov/fetch_355824_b.json")),
            ],
        }
        self.detail_calls = 0
        self.add("https://api.grants.gov/robots.txt", status=403)
        self.handle(SEARCH, self._search, method="POST")
        self.handle(FETCH, self._fetch, method="POST")

    def _search(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=self.search_data)

    def _fetch(self, request: httpx.Request) -> httpx.Response:
        opp_id = json_body(request)["opportunityId"]
        variants = self.details[opp_id]
        data = dict(variants[self.detail_calls % len(variants)])
        self.detail_calls += 1
        data["token"] = uuid.uuid4().hex
        return httpx.Response(200, json=data)


async def test_grants_gov_second_run_skips_unchanged(db_session: Session, tmp_path: Path) -> None:
    add_source(db_session, "grants_gov")
    web = GrantsGovWeb()

    first = await run(db_session, "grants_gov", web, tmp_path)
    assert first.status == RunStatus.SUCCESS
    assert (first.items_discovered, first.items_fetched, first.items_unchanged) == (3, 3, 0)
    assert snapshot_count(db_session) == 3
    doc = db_session.scalars(select(RawDocument).where(RawDocument.url.endswith("/357305"))).one()
    assert doc.url == "https://www.grants.gov/search-results-detail/357305"
    assert (tmp_path / "raw" / doc.storage_path).exists()

    # Search hits unchanged and snapshots fresh: details are not re-fetched at all.
    web.detail_calls = 0
    second = await run(db_session, "grants_gov", web, tmp_path)
    assert (second.items_discovered, second.items_fetched, second.items_unchanged) == (3, 0, 3)
    assert web.detail_calls == 0
    assert snapshot_count(db_session) == 3


async def test_grants_gov_refetch_is_unchanged_despite_volatile_fields(
    db_session: Session, tmp_path: Path
) -> None:
    source = add_source(db_session, "grants_gov")
    source.config = {**source.config, "refetch_unchanged_after_days": None}
    db_session.commit()
    web = GrantsGovWeb()

    await run(db_session, "grants_gov", web, tmp_path)
    second = await run(db_session, "grants_gov", web, tmp_path)
    # Every detail response differs byte-wise (token, list order), but not in substance.
    assert (second.items_fetched, second.items_unchanged) == (3, 3)
    assert snapshot_count(db_session) == 3


async def test_grants_gov_changed_hit_and_details_create_new_snapshot(
    db_session: Session, tmp_path: Path
) -> None:
    add_source(db_session, "grants_gov")
    web = GrantsGovWeb()
    await run(db_session, "grants_gov", web, tmp_path)

    hit = next(h for h in web.search_data["data"]["oppHits"] if h["id"] == "357305")
    hit["closeDate"] = "12/31/2026"
    web.details["357305"][0]["data"]["synopsis"]["responseDate"] = "Dec 31, 2026 12:00:00 AM EST"

    second = await run(db_session, "grants_gov", web, tmp_path)
    assert (second.items_fetched, second.items_unchanged) == (1, 2)
    assert snapshot_count(db_session) == 4


# --- EDCTP3 -------------------------------------------------------------------------------


class EdctpWeb(FakeWeb):
    def __init__(self) -> None:
        super().__init__()
        self.add(f"{EDCTP}/robots.txt", fixture_bytes("robots/global-health-edctp3.europa.eu.txt"))
        self.add("https://ec.europa.eu/robots.txt", fixture_bytes("robots/ec.europa.eu.txt"))
        self.add(LISTING, fixture_bytes("edctp3/listing_page0.html"))
        for page in range(1, 5):
            self.add(f"{LISTING}?page={page}", fixture_bytes(f"edctp3/listing_page{page}.html"))
        self.ft_topic = fixture_bytes("edctp3/ft_topic_horizon-ju-gh-edctp3-2026-03-digit-02.json")
        self.handle_prefix(FT_JSON, self._ft_topic)
        # Stand-in for the (nearly empty) EDCTP3 page, which is not saved as a fixture.
        self.add(
            f"{EDCTP}/document/download/d3d297d7-6fe2-442c-bd25-917f2b6b9dbf_en",
            fixture_bytes("edctp3/document_d3d297d7.pdf"),
            headers={"Content-Type": "application/pdf"},
        )
        self.add(f"{EDCTP}/funding/calls-proposals/support-africa-office_en", b"<main>page</main>")

    def _ft_topic(self, request: httpx.Request) -> httpx.Response:
        """One saved topic stands in for every topic; its title gets the requested topic
        id so the 33 stand-ins are distinct grants."""
        if request.headers.get("If-Modified-Since") == FT_LAST_MODIFIED:
            return httpx.Response(304)
        topic_id = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        data = json.loads(self.ft_topic)
        data["TopicDetails"]["title"] += f" ({topic_id})"
        return httpx.Response(
            200,
            content=json.dumps(data).encode(),
            headers={"Last-Modified": FT_LAST_MODIFIED, "Content-Type": "application/json"},
        )


async def test_edctp3_run_and_rerun(db_session: Session, tmp_path: Path) -> None:
    add_source(db_session, "edctp3")
    web = EdctpWeb()

    first = await run(db_session, "edctp3", web, tmp_path)
    assert first.status == RunStatus.SUCCESS
    assert (first.items_discovered, first.items_fetched, first.errors) == (35, 35, 0)
    assert snapshot_count(db_session) == 35
    # JS-rendered F&T pages were fetched as JSON; the PDF wrapper as the PDF itself.
    assert len(web.requests_to(FT_JSON)) == 33
    assert web.requests_to("https://ec.europa.eu/info/funding-tenders/opportunities/portal") == []

    web.requests.clear()
    second = await run(db_session, "edctp3", web, tmp_path)
    assert (second.items_fetched, second.items_unchanged) == (35, 35)
    assert snapshot_count(db_session) == 35
    ft_requests = web.requests_to(FT_JSON)
    assert all(r.headers.get("If-Modified-Since") == FT_LAST_MODIFIED for r in ft_requests)


async def test_item_failure_does_not_stop_run(db_session: Session, tmp_path: Path) -> None:
    add_source(db_session, "edctp3")
    web = EdctpWeb()
    web.add(f"{EDCTP}/funding/calls-proposals/support-africa-office_en", status=404)

    result = await run(db_session, "edctp3", web, tmp_path)
    assert result.status == RunStatus.PARTIAL
    assert result.errors == 1
    assert result.error_log[0]["url"].endswith("support-africa-office_en")
    assert "404" in result.error_log[0]["error"]
    assert snapshot_count(db_session) == 34


async def test_discovery_failure_fails_run(db_session: Session, tmp_path: Path) -> None:
    source = add_source(db_session, "edctp3")
    web = EdctpWeb()
    web.add(LISTING, status=500)

    result = await run(db_session, "edctp3", web, tmp_path)
    assert result.status == RunStatus.FAILED
    assert result.error_log[0]["stage"] == "discover"
    assert source.last_success_at is None
    assert source.last_run_at is not None


async def test_terms_not_reviewed_requires_force(db_session: Session, tmp_path: Path) -> None:
    add_source(db_session, "edctp3", terms_reviewed=False)
    web = EdctpWeb()
    with pytest.raises(SourceNotRunnable, match="terms not reviewed"):
        await run(db_session, "edctp3", web, tmp_path)
    assert web.requests == []

    result = await run(db_session, "edctp3", web, tmp_path, force=True, limit=2)
    assert result.status == RunStatus.SUCCESS
    assert result.items_discovered == 2


async def test_unknown_source(db_session: Session, tmp_path: Path) -> None:
    with pytest.raises(SourceNotRunnable, match="no source"):
        await run(db_session, "missing", FakeWeb(), tmp_path)


async def test_refetch_window_expires_and_resets(db_session: Session, tmp_path: Path) -> None:
    add_source(db_session, "grants_gov")
    web = GrantsGovWeb()
    await run(db_session, "grants_gov", web, tmp_path)

    # Age one snapshot beyond the 7-day window: it is re-fetched (and found unchanged).
    doc = db_session.scalars(select(RawDocument).where(RawDocument.url.endswith("/357305"))).one()
    eight_days_ago = datetime.now(UTC) - timedelta(days=8)
    doc.fetched_at = doc.last_checked_at = eight_days_ago
    db_session.commit()

    web.detail_calls = 0
    second = await run(db_session, "grants_gov", web, tmp_path)
    assert (second.items_fetched, second.items_unchanged) == (1, 3)
    assert web.detail_calls == 1
    db_session.refresh(doc)
    assert doc.last_checked_at > eight_days_ago

    # The successful re-check resets the window: no fetch on the next run.
    web.detail_calls = 0
    third = await run(db_session, "grants_gov", web, tmp_path)
    assert (third.items_fetched, third.items_unchanged) == (0, 3)
    assert web.detail_calls == 0
    assert snapshot_count(db_session) == 3
