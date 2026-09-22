"""Choose an extractor for a document, run it, and normalise the results."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fundscout.extract.base import ExtractionFailed, ExtractionInput, ExtractionOutput, Extractor
from fundscout.extract.ft_topic import FtTopicConfig, FtTopicExtractor
from fundscout.extract.llm import DEFAULT_MAX_INPUT_CHARS, LlmExtractor, LlmNotConfigured
from fundscout.extract.mapping import MappingConfig, MappingExtractor
from fundscout.extract.rules import RulesConfig, RulesExtractor
from fundscout.extract.schema import CandidateGrant
from fundscout.normalise.dates import DateOrder
from fundscout.normalise.grant import (
    NormalisationError,
    NormalisationResult,
    NormaliseOptions,
    normalise_candidate,
)

ExtractorName = Literal["llm", "mapping", "ft_topic", "rules", "skip"]


class LlmOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_selector: str = "main"
    max_input_chars: int = Field(default=DEFAULT_MAX_INPUT_CHARS, ge=1000)


_OPTION_MODELS: dict[str, type[BaseModel] | None] = {
    "llm": LlmOptions,
    "mapping": MappingConfig,
    "ft_topic": FtTopicConfig,
    "rules": RulesConfig,
    "skip": None,
}


class ExtractionRule(BaseModel):
    """First rule whose conditions all match a document decides how it is extracted."""

    model_config = ConfigDict(extra="forbid")

    match_url: str | None = None  # regex searched in the document's identity URL
    match_content_type: str | None = None  # substring of the Content-Type
    extractor: ExtractorName
    options: dict[str, Any] = {}

    @model_validator(mode="after")
    def _validate_options(self) -> "ExtractionRule":
        if self.match_url:
            re.compile(self.match_url)
        model = _OPTION_MODELS[self.extractor]
        if model is not None:
            model.model_validate(self.options)
        elif self.options:
            raise ValueError("the skip extractor takes no options")
        return self

    def matches(self, url: str, content_type: str | None) -> bool:
        if self.match_url and not re.search(self.match_url, url):
            return False
        return not (
            self.match_content_type
            and self.match_content_type.lower() not in (content_type or "").lower()
        )


class ExtractionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[ExtractionRule] = []
    date_order: DateOrder | None = None  # for numeric dates such as 03/04/2027
    funder_country: str | None = None  # ISO alpha-2; "US" lets a bare "$" mean USD

    def rule_for(self, url: str, content_type: str | None) -> ExtractionRule:
        for rule in self.rules:
            if rule.matches(url, content_type):
                return rule
        return ExtractionRule(extractor="llm")


@dataclass
class DocumentExtraction:
    output: ExtractionOutput
    results: list[NormalisationResult]


class Skipped(Exception):
    """The source's rules say this document is not extracted."""


# Builds an LlmExtractor for the given options, or raises LlmNotConfigured.
LlmFactory = Callable[[LlmOptions, Callable[[list[CandidateGrant]], None]], LlmExtractor]


def extract_document(
    doc: ExtractionInput,
    config: ExtractionConfig,
    *,
    llm_factory: LlmFactory | None,
    low_confidence_threshold: float = 0.6,
) -> DocumentExtraction:
    """Raises ExtractionFailed, LlmNotConfigured or Skipped."""
    options = NormaliseOptions(
        date_order=config.date_order,
        funder_country=config.funder_country,
        low_confidence_threshold=low_confidence_threshold,
    )

    def normalise_all(candidates: list[CandidateGrant]) -> list[NormalisationResult]:
        return [normalise_candidate(c, options) for c in candidates]

    def validate(candidates: list[CandidateGrant]) -> None:
        normalise_all(candidates)  # raises NormalisationError (a ValueError)

    rule = config.rule_for(doc.url, doc.content_type)
    extractor: Extractor
    if rule.extractor == "skip":
        raise Skipped(f"extraction skipped by rule for {doc.url}")
    if rule.extractor == "llm":
        if llm_factory is None:
            raise LlmNotConfigured("no LLM client configured")
        extractor = llm_factory(LlmOptions.model_validate(rule.options), validate)
    elif rule.extractor == "mapping":
        extractor = MappingExtractor(MappingConfig.model_validate(rule.options))
    elif rule.extractor == "ft_topic":
        extractor = FtTopicExtractor(FtTopicConfig.model_validate(rule.options))
    else:
        extractor = RulesExtractor(RulesConfig.model_validate(rule.options))

    output = extractor.extract(doc)
    try:
        results = normalise_all(output.candidates)
    except NormalisationError as exc:
        raise ExtractionFailed(
            str(exc), {"issues": [i.as_dict() for i in exc.issues], "method": output.method}
        ) from exc
    return DocumentExtraction(output, results)
