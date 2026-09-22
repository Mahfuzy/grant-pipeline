"""Normalise a CandidateGrant into a validated ExtractedGrant (SPEC §7).

Anything that is dropped, guessed or ambiguous is reported as an issue rather than
silently fixed, so M4 can turn issues into review items.
"""

import re
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import ValidationError

from fundscout.extract.schema import KEY_FIELDS, CandidateGrant, ExtractedGrant
from fundscout.normalise.amounts import parse_amount_range, parse_amount_value
from fundscout.normalise.countries import to_alpha2
from fundscout.normalise.currency import is_iso_currency, normalise_currency
from fundscout.normalise.dates import DateOrder, parse_date
from fundscout.normalise.taxonomy import TaxonomyIndex, default_taxonomy
from fundscout.normalise.text import clean_text, html_to_text, truncate

log = structlog.get_logger(__name__)

DESCRIPTION_MAX_CHARS = 1000
EVIDENCE_MAX_CHARS = 300
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TAXONOMY_FIELDS = ("regions", "org_types", "themes", "grant_types")


@dataclass(frozen=True)
class Issue:
    code: str
    field: str | None = None
    value: Any = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class NormalisationResult:
    grant: ExtractedGrant
    issues: list[Issue] = field(default_factory=list)


class NormalisationError(ValueError):
    def __init__(self, message: str, issues: list[Issue]):
        super().__init__(message)
        self.issues = issues


@dataclass(frozen=True)
class NormaliseOptions:
    date_order: DateOrder | None = None
    funder_country: str | None = None  # e.g. "US" lets a bare "$" mean USD
    low_confidence_threshold: float = 0.6


def _text(value: str | None) -> str | None:
    if value and "<" in value and ">" in value:
        return html_to_text(value)
    return clean_text(value)


def normalise_candidate(
    candidate: CandidateGrant,
    options: NormaliseOptions | None = None,
    taxonomy: TaxonomyIndex | None = None,
) -> NormalisationResult:
    options = options or NormaliseOptions()
    taxonomy = taxonomy or default_taxonomy()
    issues: list[Issue] = []
    c = candidate

    # Amounts and currency
    amount_text = clean_text(c.amount_text)
    amount_min = parse_amount_value(c.amount_min)
    amount_max = parse_amount_value(c.amount_max)
    for name, raw, parsed in (
        ("amount_min", c.amount_min, amount_min),
        ("amount_max", c.amount_max, amount_max),
    ):
        if (
            parsed is None
            and isinstance(raw, str)
            and raw.strip().lower() not in {"", "none", "n/a", "null", "0"}
        ):
            issues.append(Issue("unparsed_amount", name, raw))
    from_text = parse_amount_range(amount_text, funder_country=options.funder_country)
    if amount_min is None and amount_max is None and amount_text:
        amount_min, amount_max = from_text.minimum, from_text.maximum
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        issues.append(Issue("inconsistent_amounts", "amount_min", str(amount_min)))
        amount_min, amount_max = amount_max, amount_min

    currency: str | None = None
    if c.currency:
        result = normalise_currency(
            c.currency, context=amount_text, funder_country=options.funder_country
        )
        currency = result.code
        if result.ambiguous:
            issues.append(Issue("ambiguous_currency", "currency", c.currency))
        elif currency is None:
            issues.append(Issue("unknown_currency", "currency", c.currency))
    if currency is None and amount_text:
        currency = from_text.currency.code
        if from_text.currency.ambiguous and not any(i.code == "ambiguous_currency" for i in issues):
            issues.append(Issue("ambiguous_currency", "currency", amount_text))
    if currency is not None and not is_iso_currency(currency):
        issues.append(Issue("unknown_currency", "currency", currency))
        currency = None

    # Dates
    dates: dict[str, Any] = {}
    for name in ("opening_date", "closing_date"):
        raw_date = getattr(c, name)
        parsed_date = parse_date(raw_date, options.date_order)
        dates[name] = parsed_date.value
        if parsed_date.ambiguous:
            issues.append(Issue("ambiguous_date", name, raw_date))
        elif parsed_date.value is None and isinstance(raw_date, str) and raw_date.strip():
            issues.append(Issue("unparsed_date", name, raw_date))
    deadline_type = c.deadline_type or ("fixed" if dates["closing_date"] else "unknown")

    # Countries (region names given as countries are moved to regions)
    countries: list[str] = []
    extra_regions: list[str] = []
    for raw_country in c.countries:
        code = to_alpha2(raw_country)
        if code:
            if code not in countries:
                countries.append(code)
        elif slugs := taxonomy.resolve("regions", raw_country):
            extra_regions.extend(slugs)
        else:
            issues.append(Issue("unknown_country", "countries", raw_country))

    taxonomy_values: dict[str, list[str]] = {}
    for kind in _TAXONOMY_FIELDS:
        raw_values = list(getattr(c, kind)) + (extra_regions if kind == "regions" else [])
        resolved: list[str] = []
        for raw_value in raw_values:
            slugs = taxonomy.resolve(kind, raw_value)
            if not slugs:
                issues.append(Issue("unknown_taxonomy_value", kind, raw_value))
                log.warning("dropped taxonomy value", kind=kind, value=raw_value)
            resolved.extend(s for s in slugs if s not in resolved)
        taxonomy_values[kind] = resolved

    contact_email = clean_text(c.contact_email)
    if contact_email:
        contact_email = contact_email.removeprefix("mailto:")
        if not _EMAIL.match(contact_email):
            issues.append(Issue("invalid_email", "contact_email", contact_email))
            contact_email = None

    confidence = {
        k: min(1.0, max(0.0, float(v)))
        for k, v in c.field_confidence.items()
        if k in ExtractedGrant.model_fields
    }
    evidence = {
        k: truncate(clean_text(v), EVIDENCE_MAX_CHARS) or ""
        for k, v in c.evidence.items()
        if k in ExtractedGrant.model_fields and clean_text(v)
    }

    data: dict[str, Any] = {
        "is_grant_opportunity": c.is_grant_opportunity,
        "title": clean_text(c.title),
        "funder_name": clean_text(c.funder_name),
        "funder_website": clean_text(c.funder_website),
        "description": truncate(_text(c.description), DESCRIPTION_MAX_CHARS),
        "amount_min": amount_min,
        "amount_max": amount_max,
        "currency": currency,
        "amount_text": amount_text,
        "opening_date": dates["opening_date"],
        "closing_date": dates["closing_date"],
        "deadline_type": deadline_type,
        "deadline_text": clean_text(c.deadline_text),
        "countries": countries,
        **taxonomy_values,
        "eligibility_text": _text(c.eligibility_text),
        "application_url": clean_text(c.application_url),
        "requirements": _text(c.requirements),
        "documents_required": [d for d in (clean_text(x) for x in c.documents_required) if d],
        "funder_priorities": _text(c.funder_priorities),
        "contact_email": contact_email,
        "contact_url": clean_text(c.contact_url),
        "field_confidence": confidence,
        "evidence": evidence,
    }

    if data["is_grant_opportunity"]:
        missing = [f for f in ("title", "funder_name") if not data[f]]
        if missing:
            issues.append(Issue("missing_required_fields", detail=", ".join(missing)))
        for name in KEY_FIELDS:
            score = confidence.get(name)
            populated = data.get(name) not in (None, "", [])
            if populated and score is not None and score < options.low_confidence_threshold:
                issues.append(Issue("low_confidence", name, score))

    try:
        grant = ExtractedGrant.model_validate(data)
    except ValidationError as exc:
        raise NormalisationError(f"normalised grant is invalid: {exc}", issues) from exc
    return NormalisationResult(grant, issues)
