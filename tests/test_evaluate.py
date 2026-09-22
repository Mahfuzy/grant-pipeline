from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fundscout.extract.evaluate import field_matches, load_labels, run_eval
from fundscout.extract.llm import LlmExtractor
from fundscout.extract.pipeline import LlmOptions
from tests.fakellm import FakeClient, llm_grant

ROOT = Path(__file__).parent.parent
LABELS = ROOT / "tests/fixtures/eval/labels.yaml"


def test_label_set_size() -> None:
    labels = load_labels(LABELS)
    assert 10 <= len(labels) <= 20
    assert all((LABELS.parent / d.file).exists() for d in labels)


def test_field_matching() -> None:
    assert field_matches("title", "Health Fund (2027)", "health fund 2027")
    assert field_matches(
        "funder_name", "Global Health EDCTP3", "Global Health EDCTP3 Joint Undertaking"
    )
    assert not field_matches("title", "Health Fund", "Education Fund")
    long_title = "Mobilisation of research funds for the expansion of activities of project EBO-PEP"
    assert not field_matches("title", long_title, "Mobilisation of research funds")
    assert field_matches("amount_max", 50000, Decimal("50000.00"))
    assert field_matches("closing_date", date(2026, 7, 7), date(2026, 7, 7))
    assert field_matches("amount_min", None, None)
    assert not field_matches("amount_min", None, Decimal(1))
    assert field_matches("themes", ["health", "research"], ["research", "health"])


def test_structured_documents_score_and_llm_documents_skip() -> None:
    report = run_eval(LABELS, ROOT / "sources", llm_factory=None)
    statuses = {d.id: d.status for d in report.documents}
    assert statuses["edctp3-ebola-iba-pdf"] == "skipped"
    assert all(s == "scored" for i, s in statuses.items() if i != "edctp3-ebola-iba-pdf")
    assert report.overall.total > 50
    assert report.overall.accuracy == 1.0


def test_llm_document_is_scored_with_mismatches_reported() -> None:
    titles = [g["title"] for g in load_labels(LABELS)[-1].grants]
    grants = [
        llm_grant(
            title=t,
            funder_name="Global Health EDCTP3",
            amount_max="1000000",
            currency="EUR",
            opening_date="2026-06-30",
            closing_date="2026-07-07",
            deadline_type="fixed",
        )
        for t in titles
    ]
    grants[3]["closing_date"] = "2026-07-08"  # one deliberate error
    client = FakeClient([{"grants": grants}])

    def factory(options: LlmOptions, validate: Any) -> LlmExtractor:
        return LlmExtractor(client, "test-model", validate=validate)

    report = run_eval(LABELS, ROOT / "sources", llm_factory=factory)
    [pdf] = [d for d in report.documents if d.id == "edctp3-ebola-iba-pdf"]
    assert (pdf.status, pdf.method, pdf.extracted_grants) == ("scored", "llm", 4)
    assert [(m["grant"], m["field"]) for m in pdf.mismatches] == [(4, "closing_date")]
    assert report.fields["closing_date"].correct == report.fields["closing_date"].total - 1
    assert "closing_date" in report.render()
