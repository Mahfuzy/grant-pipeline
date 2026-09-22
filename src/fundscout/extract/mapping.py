"""Config-driven field mapping for structured (JSON API) documents.

Example (see sources/grants_gov.yaml):

    fields:
      title: data.opportunityTitle
      closing_date:
        paths:
          - data.synopsis.responseDateStr
          - {path: data.forecast.estApplicationResponseDateStr, confidence: 0.5}
      currency: {const: USD}
      amount_text:
        template: ["Award ceiling: {data.synopsis.awardCeilingFormatted}"]

For each field the first non-empty path wins. `template` lines are rendered with
`{path}` placeholders and dropped when a placeholder is empty. `$url` is the document URL.
"""

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fundscout.db.models import ExtractionMethod
from fundscout.extract.base import ExtractionFailed, ExtractionInput, ExtractionOutput
from fundscout.extract.paths import is_empty, resolve
from fundscout.extract.schema import CandidateGrant
from fundscout.normalise.text import html_to_text

_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


class PathSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    confidence: float | None = Field(default=None, ge=0, le=1)


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[PathSpec] = []
    const: Any = None
    template: list[str] = []
    transform: Literal["html"] | None = None
    join: str | None = None  # join list values into one string
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = {"paths": [value]}
        elif isinstance(value, list):
            value = {"paths": value}
        if isinstance(value, dict) and isinstance(value.get("paths"), list):
            value = dict(value)
            value["paths"] = [{"path": p} if isinstance(p, str) else p for p in value["paths"]]
        return value


class MappingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: dict[str, FieldSpec]
    default_confidence: float = Field(default=1.0, ge=0, le=1)
    # Source-specific placeholders treated as missing, e.g. Grants.gov's "none".
    empty_values: list[str] = []

    @model_validator(mode="after")
    def _known_fields(self) -> "MappingConfig":
        unknown = set(self.fields) - set(CandidateGrant.model_fields)
        if unknown:
            raise ValueError(f"unknown target fields: {sorted(unknown)}")
        return self


class MappingExtractor:
    def __init__(self, config: MappingConfig):
        self.config = config

    def _lookup(self, data: Any, path: str, url: str) -> Any:
        value = url if path == "$url" else resolve(data, path)
        empty = {v.lower() for v in self.config.empty_values}
        if isinstance(value, str) and value.strip().lower() in empty:
            return None
        if isinstance(value, list):
            value = [v for v in value if not (isinstance(v, str) and v.strip().lower() in empty)]
        return value

    def _render(self, template: list[str], data: Any, url: str) -> str | None:
        lines = []
        for line in template:
            values = {p: self._lookup(data, p, url) for p in _PLACEHOLDER.findall(line)}
            if any(is_empty(v) for v in values.values()):
                continue
            rendered = line
            for placeholder, value in values.items():
                rendered = rendered.replace("{" + placeholder + "}", str(value))
            lines.append(rendered)
        return "; ".join(lines) or None

    def extract(self, doc: ExtractionInput) -> ExtractionOutput:
        try:
            data = json.loads(doc.content)
        except ValueError as exc:
            raise ExtractionFailed(f"document is not JSON: {exc}") from exc

        values: dict[str, Any] = {}
        confidence: dict[str, float] = {}
        for name, spec in self.config.fields.items():
            value: Any = None
            score = spec.confidence
            if spec.const is not None:
                value = spec.const
            elif spec.template:
                value = self._render(spec.template, data, doc.url)
            else:
                for p in spec.paths:
                    candidate = self._lookup(data, p.path, doc.url)
                    if not is_empty(candidate):
                        value = candidate
                        score = p.confidence if p.confidence is not None else score
                        break
            if is_empty(value):
                continue
            if spec.join is not None and isinstance(value, list):
                value = spec.join.join(str(v) for v in value)
            if spec.transform == "html" and isinstance(value, str):
                value = html_to_text(value)
            values[name] = value
            confidence[name] = score if score is not None else self.config.default_confidence

        values["field_confidence"] = confidence
        try:
            candidate_grant = CandidateGrant.model_validate(values)
        except ValueError as exc:
            raise ExtractionFailed(f"mapped values are invalid: {exc}") from exc
        return ExtractionOutput(ExtractionMethod.API, [candidate_grant])
