"""Mocked Anthropic client for extraction tests (never calls the real API)."""

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from fundscout.extract.schema import LlmExtraction


@dataclass
class FakeUsage:
    input_tokens: int = 1000
    output_tokens: int = 200
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class FakeResponse:
    parsed_output: LlmExtraction | None
    stop_reason: str = "end_turn"
    usage: FakeUsage = field(default_factory=FakeUsage)


class FakeMessages:
    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, dict):
            return FakeResponse(LlmExtraction.model_validate(response))
        assert isinstance(response, FakeResponse)
        return response


class FakeClient:
    def __init__(self, responses: list[Any]):
        self.messages = FakeMessages(responses)


def llm_grant(**overrides: Any) -> dict[str, Any]:
    grant: dict[str, Any] = {
        "is_grant_opportunity": True,
        "title": None,
        "funder_name": None,
        "funder_website": None,
        "description": None,
        "amount_min": None,
        "amount_max": None,
        "currency": None,
        "amount_text": None,
        "opening_date": None,
        "closing_date": None,
        "deadline_type": "unknown",
        "deadline_text": None,
        "countries": [],
        "regions": [],
        "org_types": [],
        "themes": [],
        "grant_types": [],
        "eligibility_text": None,
        "application_url": None,
        "requirements": None,
        "documents_required": [],
        "funder_priorities": None,
        "contact_email": None,
        "contact_url": None,
        "field_confidence": [],
        "evidence": [],
    }
    grant.update(overrides)
    return grant


def schema_error() -> ValidationError:
    try:
        LlmExtraction.model_validate({"grants": [{"title": 1}]})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a validation error")
