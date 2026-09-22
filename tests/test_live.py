"""Opt-in live checks against the real sources: `uv run pytest -m live`.

Keeps traffic minimal: robots.txt plus one small search / one listing page.
"""

import json
import os

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


# Captured at import: the autouse fixture in conftest clears settings env vars per test.
_LIVE_MODEL = os.environ.get("EXTRACTION_MODEL")
_LIVE_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")
_LIVE_KEY = os.environ.get("GROQ_API_KEY" if _LIVE_PROVIDER == "groq" else "ANTHROPIC_API_KEY")


@pytest.mark.skipif(
    not (_LIVE_MODEL and _LIVE_KEY), reason="EXTRACTION_MODEL or the provider's API key unset"
)
def test_llm_extraction_live() -> None:
    """One real LLM extraction on a small saved page (costs a few thousand tokens).

    Uses LLM_PROVIDER (anthropic or groq) and its API key from the environment.
    """
    from fundscout.config import Settings
    from fundscout.extract.base import ExtractionInput
    from fundscout.extract.pipeline import LlmOptions
    from fundscout.pipeline.extraction import make_llm_factory
    from tests.fakeweb import fixture_bytes

    key_name = "groq_api_key" if _LIVE_PROVIDER == "groq" else "anthropic_api_key"
    settings = Settings.model_validate(
        {"llm_provider": _LIVE_PROVIDER, "extraction_model": _LIVE_MODEL, key_name: _LIVE_KEY}
    )
    factory = make_llm_factory(settings)
    assert factory is not None
    extractor = factory(LlmOptions(), lambda candidates: None)
    output = extractor.extract(
        ExtractionInput(
            url="https://www.global-health-edctp3.europa.eu/document/d3d297d7-6fe2-442c-bd25-917f2b6b9dbf_en",
            content=fixture_bytes("edctp3/document_d3d297d7.html"),
            content_type="text/html",
        )
    )
    assert output.usage["input_tokens"] > 0
    assert output.candidates and "Ebola" in (output.candidates[0].title or "")
