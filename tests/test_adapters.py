import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from fundscout.config import Settings
from fundscout.db.models import Source
from fundscout.fetch.http import HttpClient
from fundscout.sources.base import RunContext, SourceAdapter
from fundscout.sources.registry import build_adapter
from fundscout.sources.spec import load_source_spec
from tests.fakeweb import FakeWeb, fixture_bytes, json_body, no_sleep

ROOT = Path(__file__).parent.parent
EDCTP = "https://www.global-health-edctp3.europa.eu"
LISTING = f"{EDCTP}/funding/calls-proposals_en"
FT_TOPIC = "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/"
FT_JSON = "https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/"


def load(name: str) -> tuple[Source, SourceAdapter[Any]]:
    spec = load_source_spec(ROOT / "sources" / f"{name}.yaml")
    source = Source(**spec.model_dump())
    return source, build_adapter(spec.adapter, spec.config)


def ctx(source: Source, web: FakeWeb, limit: int | None = None) -> RunContext:
    http = HttpClient(user_agent="FundscoutBot/test", transport=web.transport(), sleep=no_sleep)
    return RunContext(source=source, run_id=None, http=http, settings=Settings(), limit=limit)  # type: ignore[arg-type]


def edctp_listing_web() -> FakeWeb:
    web = FakeWeb()
    web.add(f"{EDCTP}/robots.txt", fixture_bytes("robots/global-health-edctp3.europa.eu.txt"))
    web.add(LISTING, fixture_bytes("edctp3/listing_page0.html"))
    for page in range(1, 5):
        web.add(f"{LISTING}?page={page}", fixture_bytes(f"edctp3/listing_page{page}.html"))
    return web


async def test_html_listing_discovers_all_pages() -> None:
    source, adapter = load("edctp3")
    web = edctp_listing_web()
    items = await adapter.discover(source, ctx(source, web))

    listing_requests = [str(r.url) for r in web.requests_to(LISTING)]
    assert listing_requests == [LISTING] + [f"{LISTING}?page={p}" for p in range(1, 5)]
    # 41 listing entries: 4 share one PDF, and 3 topics are listed twice (with different
    # query strings or id case).
    assert len(items) == 35
    assert sum(len(i.metadata["entries"]) for i in items) == 41
    assert all(";" not in i.url and "?" not in i.url for i in items)

    by_url = {i.url: i for i in items}
    topic = by_url[FT_TOPIC + "horizon-ju-gh-edctp3-2026-03-digit-02"]
    assert topic.request.url == FT_JSON + "horizon-ju-gh-edctp3-2026-03-digit-02.json"
    entry = topic.metadata["entries"][0]
    assert entry["title"].startswith("Enhancing integrated research")
    assert entry["definitions"]["Status"] == ["Open"]
    assert entry["date"] == "3 December 2025"

    # Old-style links with ";key=value" search state are rewritten too.
    old = by_url[FT_TOPIC + "horizon-ju-gh-edctp3-2023-02-02-two-stage"]
    assert old.request.url == FT_JSON + "horizon-ju-gh-edctp3-2023-02-02-two-stage.json"

    pdf = by_url[f"{EDCTP}/document/d3d297d7-6fe2-442c-bd25-917f2b6b9dbf_en"]
    assert pdf.request.url == f"{EDCTP}/document/download/d3d297d7-6fe2-442c-bd25-917f2b6b9dbf_en"
    assert len(pdf.metadata["entries"]) == 4

    # Relative links are resolved against the listing page.
    assert f"{EDCTP}/funding/calls-proposals/support-africa-office_en" in by_url


async def test_html_listing_limit_stops_paging() -> None:
    source, adapter = load("edctp3")
    web = edctp_listing_web()
    items = await adapter.discover(source, ctx(source, web, limit=5))
    assert len(items) == 5
    assert len(web.requests_to(LISTING)) == 1


async def test_rss_adapter_groups_entries_by_document() -> None:
    source, _ = load("edctp3")
    adapter = build_adapter(
        "generic_rss",
        {
            "feed_url": f"{EDCTP}/node/93/rss_en",
            "url_rewrites": [
                r.model_dump()
                for r in build_adapter(
                    "generic_html_listing", load_source_spec(ROOT / "sources/edctp3.yaml").config
                ).config.url_rewrites
            ],
        },
    )
    web = FakeWeb()
    web.add(f"{EDCTP}/robots.txt", fixture_bytes("robots/global-health-edctp3.europa.eu.txt"))
    web.add(f"{EDCTP}/node/93/rss_en", fixture_bytes("edctp3/calls_rss.xml"))
    items = await adapter.discover(source, ctx(source, web))
    assert sum(len(i.metadata["entries"]) for i in items) == 30
    pdf = next(i for i in items if "/document/" in i.url)
    titles = {e["title"] for e in pdf.metadata["entries"]}
    assert len(pdf.metadata["entries"]) == 4 and len(titles) == 4
    statuses = {
        c["term"]
        for i in items
        for e in i.metadata["entries"]
        for c in e["categories"]
        if c["scheme"] == "Status"
    }
    assert statuses == {"Open", "Closed", "Completed"}


def grants_gov_web() -> FakeWeb:
    web = FakeWeb()
    web.add("https://api.grants.gov/robots.txt", status=403)
    pages = {
        0: fixture_bytes("grants_gov/search2_posted_rows5_start0.json"),
        5: fixture_bytes("grants_gov/search2_posted_rows5_start5.json"),
    }

    def search(request: httpx.Request) -> httpx.Response:
        body = json_body(request)
        data = json.loads(pages[body["startRecordNum"]])
        data["data"]["hitCount"] = 10  # the saved pages are the first 10 of 952 hits
        return httpx.Response(200, json=data)

    web.handle("https://api.grants.gov/v1/api/search2", search, method="POST")
    return web


def small_page_adapter() -> tuple[Source, SourceAdapter[Any]]:
    spec = load_source_spec(ROOT / "sources/grants_gov.yaml")
    spec.config["pagination"]["page_size"] = 5
    return Source(**spec.model_dump()), build_adapter(spec.adapter, spec.config)


async def test_api_adapter_paginates_and_builds_detail_requests() -> None:
    source, adapter = small_page_adapter()
    web = grants_gov_web()
    items = await adapter.discover(source, ctx(source, web))

    bodies = [json_body(r) for r in web.requests_to("https://api.grants.gov/v1/api/search2")]
    assert [b["startRecordNum"] for b in bodies] == [0, 5]
    assert all(b["rows"] == 5 and b["oppStatuses"] == "forecasted|posted" for b in bodies)
    assert all(b["sortBy"] == "openDate|desc" for b in bodies)

    assert len(items) == 10
    first = items[0]
    assert first.url == "https://www.grants.gov/search-results-detail/357305"
    assert first.request.url == "https://api.grants.gov/v1/api/fetchOpportunity"
    assert first.request.method == "POST"
    assert first.request.json_body == {"opportunityId": "357305"}
    assert first.metadata["hit"]["docType"] == "synopsis"


async def test_api_change_key_uses_change_fields_only() -> None:
    _, adapter = small_page_adapter()
    hit = json.loads(fixture_bytes("grants_gov/search2_posted_rows5_start0.json"))["data"][
        "oppHits"
    ][0]
    base = adapter._item(hit).change_key  # type: ignore[attr-defined]
    assert adapter._item({**hit, "unrelated": 1}).change_key == base  # type: ignore[attr-defined]
    assert adapter._item({**hit, "closeDate": "12/31/2030"}).change_key != base  # type: ignore[attr-defined]


async def test_api_limit() -> None:
    source, adapter = small_page_adapter()
    web = grants_gov_web()
    items = await adapter.discover(source, ctx(source, web, limit=3))
    assert len(items) == 3
    assert len(web.requests_to("https://api.grants.gov/v1/api/search2")) == 1


def test_invalid_config_rejected() -> None:
    with pytest.raises(ValueError):
        build_adapter("generic_html_listing", {"listing_url": "https://x.org"})
    with pytest.raises(KeyError):
        build_adapter("nope", {})
