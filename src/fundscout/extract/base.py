"""Shared extractor types."""

from dataclasses import dataclass, field
from typing import Any, Protocol

from fundscout.db.models import ExtractionMethod
from fundscout.extract.schema import CandidateGrant


@dataclass(frozen=True)
class ExtractionInput:
    url: str
    """Identity URL of the document (e.g. the public grant page)."""
    content: bytes
    content_type: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    """Discovery data for the document (listing entries, feed entry, API search hit)."""


@dataclass
class ExtractionOutput:
    method: ExtractionMethod
    candidates: list[CandidateGrant]
    model: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=dict)


class ExtractionFailed(Exception):
    """Extraction could not produce valid output (after any retries)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


class Extractor(Protocol):
    def extract(self, doc: ExtractionInput) -> ExtractionOutput: ...
