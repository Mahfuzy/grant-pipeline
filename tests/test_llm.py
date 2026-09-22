from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from fundscout.extract.base import ExtractionFailed, ExtractionInput
from fundscout.extract.llm import LlmExtractor, LlmNotConfigured
from fundscout.extract.pipeline import ExtractionConfig, LlmOptions, extract_document
from fundscout.extract.schema import CandidateGrant
from fundscout.normalise.grant import normalise_candidate
from tests.fakellm import FakeClient, FakeResponse, llm_grant, schema_error
from tests.fakeweb import fixture_bytes

PDF_URL = (
    "https://www.global-health-edctp3.europa.eu/document/d3d297d7-6fe2-442c-bd25-917f2b6b9dbf_en"
)


def pdf_doc() -> ExtractionInput:
    return ExtractionInput(
        url=PDF_URL,
        content=fixture_bytes("edctp3/document_d3d297d7.pdf"),
        content_type="application/pdf",
        metadata={"entries": [{"title": "Mobilisation of research funds for EBO-PEP"}]},
    )


def html_doc() -> ExtractionInput:
    html = b"<html><head><title>Seed Fund</title></head><body><main><p>Apply by 31 March 2027."
    return ExtractionInput(
        url="https://x.org/call",
        content=html + b"</p></main></body></html>",
        content_type="text/html",
    )


def validate(candidates: list[CandidateGrant]) -> None:
    for c in candidates:
        normalise_candidate(c)


def extractor(client: FakeClient, **kwargs: Any) -> LlmExtractor:
    return LlmExtractor(
        client, "test-model", validate=validate, today=lambda: date(2026, 9, 21), **kwargs
    )


GOOD = {
    "grants": [
        llm_grant(
            title="Mobilisation of research funds for EBO-PEP",
            funder_name="Global Health EDCTP3 Joint Undertaking",
            amount_max="1000000",
            currency="EUR",
            closing_date="2026-07-15",
            deadline_type="fixed",
            countries=["CD", "UG"],
            regions=["east-africa"],
            themes=["health", "not-a-theme"],
            field_confidence=[{"field": "title", "confidence": 0.95}],
            evidence=[{"field": "title", "snippet": "Mobilisation of research funds"}],
        )
    ]
}


def test_extracts_and_builds_prompt() -> None:
    client = FakeClient([GOOD])
    output = extractor(client).extract(pdf_doc())

    [call] = client.messages.calls
    assert call["model"] == "test-model"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    system = call["system"][0]["text"]
    assert "- health: Health" in system and "- west-africa: West Africa" in system
    user = call["messages"][0]["content"]
    assert f"Document URL: {PDF_URL}" in user
    assert "Today's date: 2026-09-21" in user
    assert "Mobilisation of research funds for EBO-PEP" in user  # listing hint
    assert "<document>\n[Page 1]\nGlobal Health EDCTP3" in user  # PDF text, not bytes

    assert output.method.value == "llm" and output.model == "test-model"
    assert output.usage["input_tokens"] == 1000 and output.usage["calls"] == 1
    [c] = output.candidates
    assert c.field_confidence == {"title": 0.95}
    assert c.evidence == {"title": "Mobilisation of research funds"}


def test_pipeline_normalises_llm_output_and_drops_unknown_slugs() -> None:
    client = FakeClient([GOOD])

    def factory(options: LlmOptions, validate: Any) -> LlmExtractor:
        return LlmExtractor(client, "test-model", validate=validate)

    result = extract_document(html_doc(), ExtractionConfig(), llm_factory=factory)
    [r] = result.results
    assert r.grant.amount_max == Decimal(1000000)
    assert r.grant.closing_date == date(2026, 7, 15)
    assert r.grant.themes == ["health"]
    assert r.grant.countries == ["CD", "UG"]
    assert [i.value for i in r.issues if i.code == "unknown_taxonomy_value"] == ["not-a-theme"]


def test_retries_once_with_validation_error() -> None:
    invalid = {"grants": [llm_grant(title="X", amount_min="-", amount_max="5")]}
    client = FakeClient([invalid, GOOD])

    def strict_validate(candidates: list[CandidateGrant]) -> None:
        for c in candidates:
            if c.amount_min == "-":
                raise ValueError("amount_min '-' is not a number")

    ex = LlmExtractor(client, "test-model", validate=strict_validate)
    output = ex.extract(html_doc())
    assert output.notes["attempts"] == 2
    assert output.usage["calls"] == 2 and output.usage["input_tokens"] == 2000
    second = client.messages.calls[1]["messages"]
    assert second[1]["role"] == "assistant"
    assert "amount_min '-' is not a number" in second[2]["content"]


def test_schema_error_is_retried() -> None:
    client = FakeClient([schema_error(), GOOD])
    output = extractor(client).extract(html_doc())
    assert output.notes["attempts"] == 2
    assert "failed validation" in client.messages.calls[1]["messages"][-1]["content"]


def test_fails_after_second_invalid_output() -> None:
    invalid = {"grants": [llm_grant(title="X", amount_min="-")]}

    def always_fail(candidates: list[CandidateGrant]) -> None:
        raise ValueError("still wrong")

    client = FakeClient([invalid, invalid])
    ex = LlmExtractor(client, "test-model", validate=always_fail)
    with pytest.raises(ExtractionFailed) as info:
        ex.extract(html_doc())
    assert len(client.messages.calls) == 2
    assert info.value.details["error"] == "still wrong"
    assert info.value.details["usage"]["calls"] == 2


def test_refusal_and_truncated_output() -> None:
    with pytest.raises(ExtractionFailed, match="refused"):
        extractor(FakeClient([FakeResponse(None, stop_reason="refusal")])).extract(html_doc())
    client = FakeClient([FakeResponse(None, stop_reason="max_tokens"), GOOD])
    assert extractor(client).extract(html_doc()).notes["attempts"] == 2


def test_missing_credentials_means_not_configured() -> None:
    error = TypeError('"Could not resolve authentication method. Expected one of api_key"')
    with pytest.raises(LlmNotConfigured):
        extractor(FakeClient([error])).extract(html_doc())
    with pytest.raises(TypeError):
        extractor(FakeClient([TypeError("unrelated bug")])).extract(html_doc())


def test_long_documents_are_truncated_and_noted() -> None:
    client = FakeClient([GOOD])
    output = extractor(client, max_input_chars=5000).extract(pdf_doc())
    assert output.notes["truncated"] is True
    assert len(client.messages.calls[0]["messages"][0]["content"]) < 6500


def test_no_llm_factory_means_not_configured() -> None:
    with pytest.raises(LlmNotConfigured):
        extract_document(html_doc(), ExtractionConfig(), llm_factory=None)
