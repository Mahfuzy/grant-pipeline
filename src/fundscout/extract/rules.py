"""Optional rule-based (CSS selector) extraction for HTML pages (SPEC §4 step 5)."""

from pydantic import BaseModel, ConfigDict, Field, model_validator
from selectolax.parser import HTMLParser

from fundscout.db.models import ExtractionMethod
from fundscout.extract.base import ExtractionInput, ExtractionOutput
from fundscout.extract.schema import CandidateGrant
from fundscout.normalise.text import clean_text

_LIST_FIELDS = {"countries", "regions", "org_types", "themes", "grant_types", "documents_required"}


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selector: str
    attribute: str | None = None  # read an attribute (e.g. href) instead of the text

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, value: object) -> object:
        return {"selector": value} if isinstance(value, str) else value


class RulesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: dict[str, Rule]
    confidence: float = Field(default=0.9, ge=0, le=1)

    @model_validator(mode="after")
    def _known_fields(self) -> "RulesConfig":
        unknown = set(self.fields) - set(CandidateGrant.model_fields)
        if unknown:
            raise ValueError(f"unknown target fields: {sorted(unknown)}")
        return self


class RulesExtractor:
    def __init__(self, config: RulesConfig):
        self.config = config

    def extract(self, doc: ExtractionInput) -> ExtractionOutput:
        tree = HTMLParser(doc.content)
        values: dict[str, object] = {}
        for name, rule in self.config.fields.items():
            nodes = tree.css(rule.selector)
            found = [
                clean_text(n.attributes.get(rule.attribute) if rule.attribute else n.text())
                for n in nodes
            ]
            texts = [t for t in found if t]
            if not texts:
                continue
            values[name] = texts if name in _LIST_FIELDS else texts[0]
        values["field_confidence"] = {k: self.config.confidence for k in values}
        return ExtractionOutput(ExtractionMethod.RULES, [CandidateGrant.model_validate(values)])
