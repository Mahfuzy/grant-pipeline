"""Groq backend for LLM extraction.

A thin client exposing the one call LlmExtractor makes, `messages.parse(...)`, on top of
Groq's OpenAI-compatible chat completions API with JSON-schema structured output. The
extractor (prompt, validation, retry, usage logging) is shared with the Anthropic backend.

Differences handled here:
- the system prompt blocks are flattened to a string (Groq has no prompt caching);
- `finish_reason` is mapped to the Anthropic stop reasons the extractor checks;
- auth errors raise LlmNotConfigured; 429/5xx are retried, honouring Retry-After;
- output that fails the schema raises pydantic's ValidationError, as the Anthropic SDK
  does, so the extractor's retry-with-error path applies.
"""

from dataclasses import dataclass
from typing import Any

import httpx
import structlog
from pydantic import BaseModel
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from fundscout.extract.llm import LlmNotConfigured

log = structlog.get_logger(__name__)

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
_STOP_REASONS = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}


class GroqError(Exception):
    def __init__(
        self, message: str, status_code: int | None = None, retry_after: float | None = None
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass
class GroqUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class GroqResponse:
    parsed_output: Any
    stop_reason: str
    usage: GroqUsage


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, GroqError) and (exc.status_code == 429 or (exc.status_code or 0) >= 500)


_backoff = wait_exponential(multiplier=2, max=60)


def _wait(state: RetryCallState) -> float:
    exc = state.outcome.exception() if state.outcome else None
    if isinstance(exc, GroqError) and exc.retry_after is not None:
        return min(exc.retry_after, 120.0)
    return float(_backoff(state))


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None


def inline_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Self-contained JSON schema: `$ref`s to `$defs` inlined and `title` keys dropped.
    Structured-output support differs between Groq models; a flat schema is the most
    widely accepted form."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return walk(defs[ref.removeprefix("#/$defs/")])
            return {k: walk(v) for k, v in node.items() if k not in ("$defs", "title")}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    result: dict[str, Any] = walk(schema)
    return result


# 400s that concern the document itself (failed per document); other 400s, 404 and 422
# mean the request setup is wrong (model name, response_format), so LLM documents are
# skipped as "not configured" rather than filling the review queue.
_DOCUMENT_ERRORS = ("context_length", "too large", "too long", "maximum context")


def _system_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    return "\n\n".join(block["text"] for block in system)


class GroqMessages:
    def __init__(self, http: httpx.Client, strict: bool):
        self.http = http
        self.strict = strict

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(4),
        wait=_wait,
        reraise=True,
    )
    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.http.post("/chat/completions", json=payload)
        if response.status_code in (401, 403):
            raise LlmNotConfigured(f"Groq rejected the API key (HTTP {response.status_code})")
        if response.status_code == 400 and "json_validate_failed" in response.text:
            # The model's output did not match the schema; treated like a validation
            # failure so the extractor retries with the error.
            return {"_invalid_output": response.text[:2000]}
        if response.status_code in (400, 404, 422) and not any(
            marker in response.text.lower() for marker in _DOCUMENT_ERRORS
        ):
            log.error(
                "groq rejected the request", status=response.status_code, body=response.text[:500]
            )
            raise LlmNotConfigured(
                f"Groq rejected the request (HTTP {response.status_code}): "
                f"{response.text[:300]}. Check EXTRACTION_MODEL; if the error is about "
                "response_format or the schema, try GROQ_STRICT_OUTPUT=false."
            )
        if response.status_code >= 400:
            log.warning("groq error", status=response.status_code, body=response.text[:500])
            raise GroqError(
                f"Groq API error {response.status_code}: {response.text[:500]}",
                response.status_code,
                _retry_after(response),
            )
        result: dict[str, Any] = response.json()
        return result

    def parse(
        self,
        *,
        model: str,
        max_tokens: int,
        system: Any,
        messages: list[dict[str, Any]],
        output_format: type[BaseModel],
    ) -> GroqResponse:
        payload = {
            "model": model,
            "max_completion_tokens": max_tokens,
            "messages": [{"role": "system", "content": _system_text(system)}, *messages],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": output_format.__name__,
                    "strict": self.strict,
                    "schema": inline_schema(output_format.model_json_schema()),
                },
            },
        }
        data = self._post(payload)
        if "_invalid_output" in data:
            return GroqResponse(None, "invalid_output", GroqUsage())
        usage = data.get("usage") or {}
        choice = data["choices"][0]
        stop_reason = _STOP_REASONS.get(choice.get("finish_reason"), "end_turn")
        content = (choice.get("message") or {}).get("content")
        parsed = None
        if stop_reason == "end_turn" and content:
            # Raises ValidationError on schema mismatch (handled by the extractor).
            parsed = output_format.model_validate_json(content)
        return GroqResponse(
            parsed,
            stop_reason,
            GroqUsage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            ),
        )


class GroqClient:
    """Minimal Groq client with the `client.messages.parse` shape LlmExtractor uses."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        strict: bool = True,
        timeout: float = 300.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )
        self.messages = GroqMessages(self.http, strict)
