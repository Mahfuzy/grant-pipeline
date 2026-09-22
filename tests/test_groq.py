"""Groq backend: request shape, response mapping and error handling (mocked HTTP)."""

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from fundscout.config import Settings
from fundscout.extract.base import ExtractionInput
from fundscout.extract.groq import GroqClient, GroqError
from fundscout.extract.llm import LlmExtractor, LlmNotConfigured
from fundscout.extract.pipeline import LlmOptions
from fundscout.extract.schema import LlmExtraction, LlmGrant
from fundscout.pipeline.extraction import make_llm_factory
from tests.fakellm import llm_grant

PAGE = b"<html><body><main><h1>Seed Fund</h1><p>Apply by 31 March 2027.</p></main></body></html>"


def completion(content: str | None, finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 300},
    }


def ok_content(**grant: Any) -> str:
    return json.dumps({"grants": [llm_grant(**grant)]})


class Recorder:
    def __init__(self, responses: list[httpx.Response]):
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer test-key"
        return self.responses.pop(0)


def client_for(responses: list[httpx.Response]) -> tuple[GroqClient, Recorder]:
    recorder = Recorder(responses)
    return GroqClient("test-key", transport=httpx.MockTransport(recorder)), recorder


def extract(client: GroqClient) -> Any:
    extractor = LlmExtractor(client, "openai/gpt-oss-120b")
    return extractor.extract(ExtractionInput("https://example.org/fund", PAGE, "text/html"))


def test_request_uses_json_schema_and_plain_system_prompt() -> None:
    content = ok_content(title="Seed Fund", funder_name="Example Trust", closing_date="2027-03-31")
    client, recorder = client_for([httpx.Response(200, json=completion(content))])
    output = extract(client)

    body = recorder.requests[0]
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    schema = body["response_format"]["json_schema"]["schema"]
    assert "$defs" not in json.dumps(schema) and "$ref" not in json.dumps(schema)
    grant = schema["properties"]["grants"]["items"]
    assert grant["additionalProperties"] is False
    assert set(grant["required"]) == set(LlmGrant.model_fields)
    system, user = body["messages"]
    assert system["role"] == "system" and "Allowed themes slugs" in system["content"]
    assert user["role"] == "user" and "Seed Fund" in user["content"]

    assert output.candidates[0].title == "Seed Fund"
    assert output.usage["input_tokens"] == 1200 and output.usage["output_tokens"] == 300


def test_schema_mismatch_is_retried_with_the_error() -> None:
    client, recorder = client_for(
        [
            httpx.Response(200, json=completion('{"grants": [{"title": 1}]}')),
            httpx.Response(200, json=completion(ok_content(title="Seed Fund"))),
        ]
    )
    output = extract(client)
    assert output.notes["attempts"] == 2
    assert "failed validation" in recorder.requests[1]["messages"][-1]["content"]


def test_groq_json_validate_failed_is_retried() -> None:
    client, _ = client_for(
        [
            httpx.Response(400, json={"error": {"code": "json_validate_failed"}}),
            httpx.Response(200, json=completion(ok_content(title="Seed Fund"))),
        ]
    )
    assert extract(client).candidates[0].title == "Seed Fund"


def test_length_finish_reason_maps_to_max_tokens() -> None:
    client, _ = client_for([httpx.Response(200, json=completion('{"gr', "length"))])
    response = client.messages.parse(
        model="m", max_tokens=10, system="s", messages=[], output_format=LlmExtraction
    )
    assert response.stop_reason == "max_tokens" and response.parsed_output is None


def test_invalid_json_raises_validation_error() -> None:
    client, _ = client_for([httpx.Response(200, json=completion("not json"))])
    with pytest.raises(ValidationError):
        client.messages.parse(
            model="m", max_tokens=10, system="s", messages=[], output_format=LlmExtraction
        )


def test_bad_key_is_not_configured() -> None:
    client, _ = client_for([httpx.Response(401, json={"error": "invalid api key"})])
    with pytest.raises(LlmNotConfigured):
        extract(client)


def test_rate_limit_is_retried_after_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)
    client, recorder = client_for(
        [
            httpx.Response(429, json={"error": "rate limited"}, headers={"retry-after": "7"}),
            httpx.Response(200, json=completion(ok_content(title="Seed Fund"))),
        ]
    )
    assert extract(client).candidates[0].title == "Seed Fund"
    assert len(recorder.requests) == 2
    assert slept == [7.0]


def test_setup_errors_are_not_configured() -> None:
    for response in (
        httpx.Response(404, json={"error": "model not found"}),
        httpx.Response(400, json={"error": {"message": "response_format json_schema unsupported"}}),
    ):
        client, _ = client_for([response])
        with pytest.raises(LlmNotConfigured, match="GROQ_STRICT_OUTPUT"):
            extract(client)


def test_document_too_long_is_a_document_failure() -> None:
    client, _ = client_for(
        [httpx.Response(400, json={"error": {"message": "context_length_exceeded"}})]
    )
    with pytest.raises(GroqError, match="400"):
        extract(client)


def test_factory_without_groq_key_is_not_configured() -> None:
    settings = Settings(llm_provider="groq", extraction_model="openai/gpt-oss-120b")
    factory = make_llm_factory(settings)
    assert factory is not None
    with pytest.raises(LlmNotConfigured, match="GROQ_API_KEY"):
        factory(LlmOptions(), lambda candidates: None)


def test_factory_builds_groq_client() -> None:
    settings = Settings.model_validate(
        {"llm_provider": "groq", "extraction_model": "m", "groq_api_key": "k"}
    )
    factory = make_llm_factory(settings)
    assert factory is not None
    extractor = factory(LlmOptions(), lambda candidates: None)
    assert isinstance(extractor.client, GroqClient)
