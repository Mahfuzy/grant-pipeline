"""Extraction schemas.

- `CandidateGrant`: what an extractor produces - loose, source-shaped values (amounts
  and dates as text, taxonomy values as labels or aliases, countries as names or codes).
- `ExtractedGrant` (SPEC §6.1): the validated, normalised result. Produced from a
  candidate by `fundscout.normalise.grant.normalise_candidate`.
- `LlmExtraction` / `LlmGrant`: the strict schema the LLM fills in via structured
  outputs. Structured outputs cannot express open-ended maps, so confidence and evidence
  are lists of {field, ...} pairs there.
"""

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DeadlineType = Literal["fixed", "rolling", "multiple", "unknown"]

# Fields whose confidence drives `low_confidence` review items (SPEC §8.3).
KEY_FIELDS = (
    "title",
    "funder_name",
    "amount_min",
    "amount_max",
    "currency",
    "opening_date",
    "closing_date",
    "deadline_type",
    "application_url",
)


class ExtractedGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_grant_opportunity: bool
    title: str | None
    funder_name: str | None
    funder_website: str | None
    description: str | None = Field(max_length=1200)
    amount_min: Decimal | None
    amount_max: Decimal | None
    currency: str | None = Field(pattern=r"^[A-Z]{3}$")
    amount_text: str | None
    opening_date: date | None
    closing_date: date | None
    deadline_type: DeadlineType
    deadline_text: str | None
    countries: list[str]
    regions: list[str]
    org_types: list[str]
    themes: list[str]
    grant_types: list[str]
    eligibility_text: str | None
    application_url: str | None
    # Phase 2
    requirements: str | None
    documents_required: list[str]
    funder_priorities: str | None
    contact_email: str | None
    contact_url: str | None
    # Quality
    field_confidence: dict[str, float]
    evidence: dict[str, str]

    @field_validator("countries")
    @classmethod
    def _alpha2(cls, values: list[str]) -> list[str]:
        for v in values:
            if len(v) != 2 or not v.isupper():
                raise ValueError(f"{v!r} is not an ISO 3166-1 alpha-2 code")
        return values

    @field_validator("field_confidence")
    @classmethod
    def _confidence_range(cls, values: dict[str, float]) -> dict[str, float]:
        for k, v in values.items():
            if not 0 <= v <= 1:
                raise ValueError(f"confidence for {k!r} must be between 0 and 1")
        return values

    @model_validator(mode="after")
    def _consistent_amounts(self) -> "ExtractedGrant":
        if (
            self.amount_min is not None
            and self.amount_max is not None
            and self.amount_min > self.amount_max
        ):
            raise ValueError("amount_min is greater than amount_max")
        return self


class CandidateGrant(BaseModel):
    """Extractor output before normalisation. Every field is optional and loosely typed."""

    model_config = ConfigDict(extra="forbid")

    is_grant_opportunity: bool = True
    title: str | None = None
    funder_name: str | None = None
    funder_website: str | None = None
    description: str | None = None
    amount_min: str | int | float | Decimal | None = None
    amount_max: str | int | float | Decimal | None = None
    currency: str | None = None
    amount_text: str | None = None
    opening_date: str | date | None = None
    closing_date: str | date | None = None
    deadline_type: DeadlineType | None = None
    deadline_text: str | None = None
    countries: list[str] = []
    regions: list[str] = []
    org_types: list[str] = []
    themes: list[str] = []
    grant_types: list[str] = []
    eligibility_text: str | None = None
    application_url: str | None = None
    requirements: str | None = None
    documents_required: list[str] = []
    funder_priorities: str | None = None
    contact_email: str | None = None
    contact_url: str | None = None
    field_confidence: dict[str, float] = {}
    evidence: dict[str, str] = {}


# --- LLM wire schema -----------------------------------------------------------------------


class FieldConfidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    confidence: float = Field(description="0 to 1")


class FieldEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    snippet: str = Field(description="Short quote from the page supporting the value")


class LlmGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_grant_opportunity: bool
    title: str | None
    funder_name: str | None
    funder_website: str | None
    description: str | None = Field(description="Concise summary, at most ~1000 characters")
    amount_min: str | None = Field(description="Smallest award as a number, e.g. '10000'")
    amount_max: str | None = Field(description="Largest award as a number, e.g. '50000'")
    currency: str | None = Field(description="ISO 4217 code, e.g. USD, EUR, GHS")
    amount_text: str | None = Field(description="Amount exactly as written on the page")
    opening_date: str | None = Field(description="YYYY-MM-DD, only if stated")
    closing_date: str | None = Field(description="YYYY-MM-DD, only if stated")
    deadline_type: DeadlineType
    deadline_text: str | None = Field(description="Deadline exactly as written")
    countries: list[str] = Field(description="ISO 3166-1 alpha-2 codes of eligible countries")
    regions: list[str]
    org_types: list[str]
    themes: list[str]
    grant_types: list[str]
    eligibility_text: str | None
    application_url: str | None
    requirements: str | None
    documents_required: list[str]
    funder_priorities: str | None
    contact_email: str | None
    contact_url: str | None
    field_confidence: list[FieldConfidence]
    evidence: list[FieldEvidence]

    def to_candidate(self) -> CandidateGrant:
        data: dict[str, Any] = self.model_dump(exclude={"field_confidence", "evidence"})
        data["field_confidence"] = {c.field: c.confidence for c in self.field_confidence}
        data["evidence"] = {e.field: e.snippet for e in self.evidence}
        return CandidateGrant.model_validate(data)


class LlmExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grants: list[LlmGrant] = Field(
        description="One entry per distinct grant opportunity on the page; empty if none"
    )
