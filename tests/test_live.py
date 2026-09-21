"""Opt-in live checks against the real sources: `uv run pytest -m live`.

Keeps traffic minimal: robots.txt plus one small search / one listing page.
"""

import json

import pytest

from fundscout.fetch.http import FetchRequest, HttpClient

pytestmark = pytest.mark.live

UA = "FundscoutBot/0.1.0 (+https://github.com/Mahfuzy/grant-pipeline; live test)"


async def test_grants_gov_search2_live() -> None:
    async with HttpClient(user_agent=UA) as http:
        doc = await http.fetch(
            FetchRequest(
                "https://api.grants.gov/v1/api/search2",
                method="POST",
                json_body={"rows": 1, "oppStatuses": "forecasted|posted"},
            )
        )
    data = json.loads(doc.content)["data"]
    assert data["hitCount"] > 0
    assert {"id", "title", "closeDate", "oppStatus"} <= data["oppHits"][0].keys()


async def test_edctp3_listing_live() -> None:
    async with HttpClient(user_agent=UA) as http:
        doc = await http.fetch(
            FetchRequest("https://www.global-health-edctp3.europa.eu/funding/calls-proposals_en"),
            min_interval=10,
            reject_markers=["<title>Sorry - "],
        )
    assert b"ecl-content-item" in doc.content
