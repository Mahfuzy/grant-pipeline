import httpx
import pytest

from fundscout.fetch.http import (
    FetchRequest,
    HttpClient,
    HttpStatusError,
    RetryableError,
    RobotsDisallowed,
)
from tests.fakeweb import FakeWeb, fixture_bytes, no_sleep

UA = "FundscoutBot/0.1.0 (+mailto:ops@example.org)"
EDCTP = "https://www.global-health-edctp3.europa.eu"


def client(web: FakeWeb, **kwargs: object) -> HttpClient:
    return HttpClient(user_agent=UA, transport=web.transport(), sleep=no_sleep, **kwargs)  # type: ignore[arg-type]


def edctp_web() -> FakeWeb:
    web = FakeWeb()
    web.add(f"{EDCTP}/robots.txt", fixture_bytes("robots/global-health-edctp3.europa.eu.txt"))
    return web


async def test_sends_user_agent_and_checks_robots_once() -> None:
    web = edctp_web()
    web.add(f"{EDCTP}/funding/calls-proposals_en", b"<html>ok</html>")
    async with client(web) as http:
        doc = await http.fetch(FetchRequest(f"{EDCTP}/funding/calls-proposals_en"))
        await http.fetch(FetchRequest(f"{EDCTP}/funding/calls-proposals_en"))
    assert doc.content == b"<html>ok</html>"
    assert all(r.headers["User-Agent"] == UA for r in web.requests)
    assert len(web.requests_to(f"{EDCTP}/robots.txt")) == 1


async def test_robots_disallowed_is_not_fetched() -> None:
    web = edctp_web()
    async with client(web) as http:
        with pytest.raises(RobotsDisallowed):
            await http.fetch(FetchRequest(f"{EDCTP}/search/x"))
    assert web.requests_to(f"{EDCTP}/search") == []


async def test_robots_403_means_allowed() -> None:
    web = FakeWeb()
    web.add("https://api.example.org/robots.txt", status=403)
    web.add("https://api.example.org/v1/search", b"{}", method="POST")
    async with client(web) as http:
        doc = await http.fetch(
            FetchRequest("https://api.example.org/v1/search", method="POST", json_body={})
        )
    assert doc.status_code == 200


async def test_retries_429_honouring_retry_after() -> None:
    web = FakeWeb()
    web.add("https://x.org/robots.txt", status=404)
    responses = iter(
        [httpx.Response(429, headers={"Retry-After": "12"}), httpx.Response(200, content=b"ok")]
    )
    web.handle("https://x.org/p", lambda _r: next(responses))
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    async with HttpClient(user_agent=UA, transport=web.transport(), sleep=sleep) as http:
        doc = await http.fetch(FetchRequest("https://x.org/p"), min_interval=1)
    assert doc.content == b"ok"
    assert 12.0 in slept


async def test_gives_up_after_max_attempts() -> None:
    web = FakeWeb()
    web.add("https://x.org/robots.txt", status=404)
    web.add("https://x.org/p", status=503)
    async with client(web, max_attempts=3) as http:
        with pytest.raises(RetryableError):
            await http.fetch(FetchRequest("https://x.org/p"))
    assert len(web.requests_to("https://x.org/p")) == 3


async def test_404_is_not_retried() -> None:
    web = FakeWeb()
    web.add("https://x.org/robots.txt", status=404)
    async with client(web) as http:
        with pytest.raises(HttpStatusError) as info:
            await http.fetch(FetchRequest("https://x.org/missing"))
    assert info.value.status == 404
    assert len(web.requests_to("https://x.org/missing")) == 1


async def test_block_page_is_retried_not_returned() -> None:
    web = edctp_web()
    sorry = fixture_bytes("edctp3/sorry_429.html")
    listing = fixture_bytes("edctp3/listing_page0.html")
    pages = iter([sorry, listing])
    web.handle(
        f"{EDCTP}/funding/calls-proposals_en",
        lambda _r: httpx.Response(200, content=next(pages)),
    )
    async with client(web) as http:
        doc = await http.fetch(
            FetchRequest(f"{EDCTP}/funding/calls-proposals_en"),
            reject_markers=["<title>Sorry - "],
        )
    assert doc.content == listing


async def test_conditional_get_and_304() -> None:
    web = FakeWeb()
    web.add("https://x.org/robots.txt", status=404)
    web.add("https://x.org/p", status=304)
    async with client(web) as http:
        doc = await http.fetch(
            FetchRequest("https://x.org/p"), etag='"abc"', last_modified="Fri, 04 Sep 2026"
        )
    assert doc.not_modified
    request = web.requests_to("https://x.org/p")[0]
    assert request.headers["If-None-Match"] == '"abc"'
    assert request.headers["If-Modified-Since"] == "Fri, 04 Sep 2026"
