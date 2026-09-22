"""Deterministic extractors (mapping, F&T topic, rules) and text preparation."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from fundscout.extract.base import ExtractionFailed, ExtractionInput
from fundscout.extract.ft_topic import FtTopicConfig, FtTopicExtractor
from fundscout.extract.paths import resolve
from fundscout.extract.pipeline import ExtractionConfig, Skipped, extract_document
from fundscout.extract.rules import RulesConfig, RulesExtractor
from fundscout.extract.text import document_text, prepare_text
from fundscout.sources.registry import get_adapter_class
from fundscout.sources.spec import load_source_spec
from tests.fakeweb import fixture_bytes

ROOT = Path(__file__).parent.parent
FT_URL = (
    "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/"
    "topic-details/horizon-ju-gh-edctp3-2026-03-digit-02"
)


def source_extraction(name: str) -> ExtractionConfig:
    spec = load_source_spec(ROOT / "sources" / f"{name}.yaml")
    return get_adapter_class(spec.adapter).validate_config(spec.config).extraction


def grants_gov(fixture: str, opp_id: str) -> Any:
    doc = ExtractionInput(
        url=f"https://www.grants.gov/search-results-detail/{opp_id}",
        content=fixture_bytes(f"grants_gov/{fixture}.json"),
        content_type="application/json",
    )
    return extract_document(doc, source_extraction("grants_gov"), llm_factory=None)


def test_paths() -> None:
    data = {"a": {"b": [{"c": 1}, {"c": 2}, {"c": ""}]}}
    assert resolve(data, "a.b[*].c") == [1, 2]
    assert resolve(data, "a.b[1].c") == 2
    assert resolve(data, "a.x") is None
    assert resolve(data, "a.b[9].c") is None


def test_grants_gov_posted_synopsis() -> None:
    result = grants_gov("fetch_357658", "357658")
    [r] = result.results
    g = r.grant
    assert result.output.method.value == "api"
    assert g.title.startswith("Limited Competition: Small Grant Program for the NCATS")
    assert g.funder_name == "National Institutes of Health"
    assert (g.amount_min, g.amount_max, g.currency) == (None, Decimal(50000), "USD")
    assert g.amount_text == "Award ceiling: USD 50000"
    assert (g.opening_date, g.closing_date) == (date(2024, 12, 11), date(2026, 10, 19))
    assert g.deadline_type == "fixed"
    assert g.themes == ["health"]
    assert set(g.org_types) == {
        "government-public-body",
        "sme-for-profit",
        "nonprofit-ngo",
        "university-research-institution",
    }
    assert g.application_url == "https://www.grants.gov/search-results-detail/357658"
    assert g.contact_email == "grantsinfo@nih.gov"
    assert g.field_confidence["closing_date"] == 1.0
    # Grants.gov categories with no taxonomy equivalent are reported, not silently lost.
    dropped = {i.value for i in r.issues if i.code == "unknown_taxonomy_value"}
    assert any(v.startswith("Others (see text field") for v in dropped)


def test_grants_gov_none_amounts() -> None:
    [r] = grants_gov("fetch_357305", "357305").results
    assert (r.grant.amount_min, r.grant.amount_max, r.grant.amount_text) == (None, None, None)
    assert not [i for i in r.issues if i.code == "unparsed_amount"]


def test_grants_gov_forecast_uses_estimates_with_lower_confidence() -> None:
    [r] = grants_gov("fetch_355824_a", "355824").results
    g = r.grant
    assert g.title == "Making America Healthy Again by Addressing Dementia Disparities"
    assert (g.amount_min, g.amount_max) == (Decimal(450000), Decimal(600000))
    assert g.amount_text == (
        "Award floor: USD 450000; Award ceiling: USD 600000; "
        "Estimated total funding: USD 5000000; Expected number of awards: 9"
    )
    assert (g.opening_date, g.closing_date) == (date(2025, 4, 14), date(2025, 6, 23))
    assert g.field_confidence["closing_date"] == 0.5
    assert g.deadline_text.startswith("Applications must be submitted electronically")
    assert {i.field for i in r.issues if i.code == "low_confidence"} == {
        "opening_date",
        "closing_date",
    }


def test_ft_topic() -> None:
    extractor = FtTopicExtractor(
        FtTopicConfig(funder_name="Global Health EDCTP3 Joint Undertaking")
    )
    doc = ExtractionInput(
        url=FT_URL,
        content=fixture_bytes("edctp3/ft_topic_horizon-ju-gh-edctp3-2026-03-digit-02.json"),
        content_type="application/json",
    )
    output = extractor.extract(doc)
    [c] = output.candidates
    assert c.title == (
        "Enhancing integrated research and healthcare in sub-Saharan Africa through "
        "digital innovation and Artificial Intelligence"
    )
    # Only this topic's budget entry is used, not the sibling topic in the same call.
    assert (c.amount_min, c.amount_max, c.currency) == (
        Decimal(2250000),
        Decimal(2250000),
        "EUR",
    )
    assert c.amount_text == (
        "EUR 2,250,000 per grant; topic budget EUR 18,000,000; 8 grants expected"
    )
    assert (c.opening_date, c.closing_date) == (date(2026, 1, 14), date(2026, 9, 2))
    assert c.application_url == FT_URL
    assert "health" in c.themes
    assert c.description and c.description.startswith("Expected Impact")
    assert output.notes == {
        "identifier": "HORIZON-JU-GH-EDCTP3-2026-03-DIGIT-02",
        "portal_status": "Closed",
    }


def test_ft_topic_rejects_other_json() -> None:
    doc = ExtractionInput(url=FT_URL, content=b'{"x": 1}', content_type="application/json")
    with pytest.raises(ExtractionFailed):
        FtTopicExtractor(FtTopicConfig()).extract(doc)


def test_edctp3_rules_route_documents() -> None:
    config = source_extraction("edctp3")
    assert config.rule_for(FT_URL, "application/json").extractor == "ft_topic"
    pdf = "https://www.global-health-edctp3.europa.eu/document/d3d297d7_en"
    assert config.rule_for(pdf, "application/pdf").extractor == "llm"
    assert config.date_order == "DMY"


def test_skip_rule() -> None:
    config = ExtractionConfig.model_validate({"rules": [{"extractor": "skip"}]})
    doc = ExtractionInput(url="https://x.org", content=b"x", content_type="text/html")
    with pytest.raises(Skipped):
        extract_document(doc, config, llm_factory=None)


def test_rules_extractor() -> None:
    html = b"""<html><body><h1 class="t"> Seed Fund </h1>
    <span class="amt">Up to EUR 10,000</span><span class="dl">31 March 2027</span>
    <a class="apply" href="https://x.org/apply">Apply</a>
    <ul class="themes"><li>Health</li><li>Education</li></ul></body></html>"""
    config = RulesConfig.model_validate(
        {
            "fields": {
                "title": "h1.t",
                "amount_text": ".amt",
                "closing_date": ".dl",
                "application_url": {"selector": "a.apply", "attribute": "href"},
                "themes": ".themes li",
            }
        }
    )
    [c] = (
        RulesExtractor(config)
        .extract(ExtractionInput(url="https://x.org", content=html, content_type="text/html"))
        .candidates
    )
    assert (c.title, c.amount_text, c.closing_date) == (
        "Seed Fund",
        "Up to EUR 10,000",
        "31 March 2027",
    )
    assert c.application_url == "https://x.org/apply"
    assert c.themes == ["Health", "Education"]


def test_pdf_text() -> None:
    text = document_text(fixture_bytes("edctp3/document_d3d297d7.pdf"), "application/pdf")
    assert text.startswith("[Page 1]\nGlobal Health EDCTP3")
    assert "HORIZON-JU-GH-EDCTP3-2026-01-EBOLA-IBA" in text
    assert "[Page 25]" in text


def test_html_text_is_main_content() -> None:
    html = fixture_bytes("edctp3/document_d3d297d7.html")
    text = document_text(html, "text/html; charset=UTF-8")
    assert "Mobilisation of research funds in response to the Ebola Bundibugyo outbreak" in text
    assert "Skip to main content" not in text


def test_prepare_text_keeps_opening_and_key_paragraphs() -> None:
    filler = [f"Background paragraph {i} about the programme history." * 5 for i in range(200)]
    paragraphs = [
        "Call title and summary.",
        *filler[:100],
        "The deadline is 31 March 2027.",
        *filler[100:],
        "Eligible applicants: NGOs in Ghana.",
    ]
    text = "\n\n".join(paragraphs)
    prepared = prepare_text(text, 6000)
    assert prepared.truncated and len(prepared.text) <= 6000
    assert prepared.text.startswith("Call title and summary.")
    assert "The deadline is 31 March 2027." in prepared.text
    assert "Eligible applicants: NGOs in Ghana." in prepared.text
    assert "[…]" in prepared.text
    assert not prepare_text("short", 6000).truncated
