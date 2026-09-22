"""Extraction evaluation against a hand-labelled set (SPEC §12, M3).

Runs each labelled document through its source's configured extraction and reports
per-field accuracy. Only fields present in a label are scored. Text fields match when
their normalised forms are near-identical (ratio >= 90; funder names may also differ by
extra words such as "Joint Undertaking"); amounts, dates and enums must be equal; lists
must contain the same set. Documents needing the LLM are
skipped (and reported) when no LLM is configured.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict
from rapidfuzz import fuzz

from fundscout.extract.base import ExtractionInput
from fundscout.extract.llm import LlmNotConfigured
from fundscout.extract.pipeline import ExtractionConfig, LlmFactory, extract_document
from fundscout.extract.schema import ExtractedGrant
from fundscout.normalise.text import normalise_key
from fundscout.sources.registry import get_adapter_class
from fundscout.sources.spec import load_source_spec

TEXT_FIELDS = {"title", "funder_name", "application_url", "deadline_text"}
TEXT_MATCH = 90


class LabelledDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    file: str
    url: str
    content_type: str
    grants: list[dict[str, Any]]


def load_labels(path: Path) -> list[LabelledDocument]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [LabelledDocument.model_validate(d) for d in raw]


def _normalise(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name in ("amount_min", "amount_max"):
        return Decimal(str(value))
    if name in ("opening_date", "closing_date"):
        return value if isinstance(value, date) else date.fromisoformat(str(value))
    if isinstance(value, list):
        return {str(v) for v in value}
    return value


def field_matches(name: str, expected: Any, actual: Any) -> bool:
    expected, actual = _normalise(name, expected), _normalise(name, actual)
    if name in TEXT_FIELDS and isinstance(expected, str) and isinstance(actual, str):
        a, b = normalise_key(expected), normalise_key(actual)
        if name == "funder_name":
            # "Global Health EDCTP3" and "... Joint Undertaking" are the same funder.
            return fuzz.token_set_ratio(a, b) >= TEXT_MATCH
        # Whole-string similarity: a truncated title must not count as correct.
        return fuzz.ratio(a, b) >= TEXT_MATCH
    return bool(expected == actual)


def _pair(expected: list[dict[str, Any]], actual: list[ExtractedGrant]) -> list[Any]:
    """Match labelled grants to extracted grants by title similarity (greedy)."""
    remaining = list(actual)
    pairs: list[Any] = []
    for label in expected:
        best, best_score = None, -1.0
        for grant in remaining:
            score = fuzz.token_set_ratio(
                normalise_key(str(label.get("title") or "")), normalise_key(grant.title or "")
            )
            if score > best_score:
                best, best_score = grant, score
        if best is not None:
            remaining.remove(best)
        pairs.append(best)
    return pairs


@dataclass
class FieldScore:
    correct: int = 0
    total: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


@dataclass
class DocumentReport:
    id: str
    status: str  # "scored", "skipped" or "failed"
    method: str | None = None
    expected_grants: int = 0
    extracted_grants: int = 0
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass
class EvalReport:
    fields: dict[str, FieldScore]
    documents: list[DocumentReport]

    @property
    def overall(self) -> FieldScore:
        return FieldScore(
            sum(s.correct for s in self.fields.values()),
            sum(s.total for s in self.fields.values()),
        )

    def render(self) -> str:
        lines = ["Per-field accuracy", "------------------"]
        for name, score in sorted(self.fields.items()):
            lines.append(f"{name:<16} {score.correct:>3}/{score.total:<3} {score.accuracy:7.1%}")
        o = self.overall
        lines += [f"{'overall':<16} {o.correct:>3}/{o.total:<3} {o.accuracy:7.1%}", ""]
        lines += ["Documents", "---------"]
        for d in self.documents:
            detail = d.error or (
                f"{d.method}, grants {d.extracted_grants}/{d.expected_grants}, "
                f"{len(d.mismatches)} mismatches"
            )
            lines.append(f"{d.id:<32} {d.status:<8} {detail}")
            for m in d.mismatches:
                lines.append(
                    f"    grant {m['grant']}: {m['field']}: expected {m['expected']!r}, "
                    f"got {m['actual']!r}"
                )
        return "\n".join(lines)


def _source_config(sources_dir: Path, name: str) -> ExtractionConfig:
    spec = load_source_spec(sources_dir / f"{name}.yaml")
    return get_adapter_class(spec.adapter).validate_config(spec.config).extraction


def run_eval(
    labels_path: Path,
    sources_dir: Path,
    *,
    llm_factory: LlmFactory | None,
    force_llm: bool = False,
) -> EvalReport:
    """Score extraction against the labels. force_llm sends every document through the
    LLM (ignoring the sources' mapping rules) to measure LLM accuracy on the whole set."""
    scores: dict[str, FieldScore] = defaultdict(FieldScore)
    documents: list[DocumentReport] = []
    for labelled in load_labels(labels_path):
        report = DocumentReport(labelled.id, "scored", expected_grants=len(labelled.grants))
        documents.append(report)
        doc = ExtractionInput(
            url=labelled.url,
            content=(labels_path.parent / labelled.file).read_bytes(),
            content_type=labelled.content_type,
        )
        grants: list[ExtractedGrant] = []
        try:
            config = _source_config(sources_dir, labelled.source)
            if force_llm:
                config = config.model_copy(update={"rules": []})  # default rule: LLM
            extraction = extract_document(doc, config, llm_factory=llm_factory)
            grants = [r.grant for r in extraction.results]
            report.method = extraction.output.method.value
        except LlmNotConfigured:
            report.status, report.error = "skipped", "LLM not configured"
            continue
        except Exception as exc:
            # Counted as all-wrong for this document, not hidden.
            report.status, report.error = "failed", repr(exc)[:300]

        report.extracted_grants = len(grants)
        pairs = _pair(labelled.grants, grants)
        for index, (label, grant) in enumerate(zip(labelled.grants, pairs, strict=True), 1):
            for name, expected in label.items():
                actual = getattr(grant, name) if grant is not None else None
                ok = grant is not None and field_matches(name, expected, actual)
                scores[name].total += 1
                scores[name].correct += int(ok)
                if not ok:
                    report.mismatches.append(
                        {"grant": index, "field": name, "expected": expected, "actual": actual}
                    )
    return EvalReport(dict(scores), documents)
