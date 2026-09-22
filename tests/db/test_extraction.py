from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fundscout.config import Settings
from fundscout.db.models import (
    ExtractionMethod,
    ExtractionResult,
    RawDocument,
    ReviewItem,
    ReviewReason,
    RunStatus,
    Source,
)
from fundscout.extract.llm import LlmExtractor
from fundscout.extract.pipeline import LlmOptions
from fundscout.fetch.storage import LocalRawStorage
from fundscout.pipeline.extraction import reprocess_source
from tests.db.test_runner import EdctpWeb, GrantsGovWeb, add_source, run
from tests.fakellm import FakeClient, llm_grant


def count(session: Session, model: type[Any]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def llm_factory(client: FakeClient) -> Any:
    def factory(options: LlmOptions, validate: Any) -> LlmExtractor:
        return LlmExtractor(client, "test-model", validate=validate)

    return factory


PDF_GRANTS = {
    "grants": [
        llm_grant(
            title=f"Mobilisation of research funds for project {name}",
            funder_name="Global Health EDCTP3 Joint Undertaking",
            deadline_type="unknown",
        )
        for name in ("EBO-PEP", "Ebola PREP-TBOX", "EPoCA", "other")
    ]
}
NOT_A_GRANT = {
    "grants": [llm_grant(is_grant_opportunity=False, title="Support for an Africa office")]
}


async def test_grants_gov_run_extracts_new_documents(db_session: Session, tmp_path: Path) -> None:
    add_source(db_session, "grants_gov")
    web = GrantsGovWeb()
    await run(db_session, "grants_gov", web, tmp_path)

    results = db_session.scalars(select(ExtractionResult)).all()
    assert len(results) == 3
    assert {r.method for r in results} == {ExtractionMethod.API}
    titles = {r.output["grants"][0]["title"] for r in results}
    assert "Making America Healthy Again by Addressing Dementia Disparities" in titles

    # Unchanged documents are not re-extracted.
    await run(db_session, "grants_gov", web, tmp_path)
    assert count(db_session, ExtractionResult) == 3


async def test_edctp3_run_uses_ft_topic_and_llm(db_session: Session, tmp_path: Path) -> None:
    add_source(db_session, "edctp3")
    client = FakeClient([PDF_GRANTS, NOT_A_GRANT])
    result = await run(db_session, "edctp3", EdctpWeb(), tmp_path, llm_factory=llm_factory(client))
    assert result.status == RunStatus.SUCCESS

    by_method: dict[ExtractionMethod, list[ExtractionResult]] = {}
    for r in db_session.scalars(select(ExtractionResult)):
        by_method.setdefault(r.method, []).append(r)
    assert len(by_method[ExtractionMethod.API]) == 33  # F&T topic JSON, no LLM
    assert len(by_method[ExtractionMethod.LLM]) == 2  # the PDF and the HTML page
    assert len(client.messages.calls) == 2

    pdf = next(r for r in by_method[ExtractionMethod.LLM] if len(r.output["grants"]) == 4)
    assert pdf.model == "test-model"
    assert pdf.output["usage"]["input_tokens"] == 1000
    raw = db_session.get(RawDocument, pdf.raw_document_id)
    assert raw is not None and raw.discovery_metadata is not None
    assert len(raw.discovery_metadata["entries"]) == 4  # the four listing entries (hints)


async def test_extraction_failure_creates_review_item(db_session: Session, tmp_path: Path) -> None:
    add_source(db_session, "edctp3")
    invalid = {"grants": [llm_grant(title="X", amount_min="-", countries=["Atlantis"])]}

    def failing_factory(options: LlmOptions, validate: Any) -> LlmExtractor:
        def reject(candidates: Any) -> None:
            raise ValueError("amount_min '-' is not a number")

        return LlmExtractor(FakeClient([invalid] * 4), "test-model", validate=reject)

    result = await run(db_session, "edctp3", EdctpWeb(), tmp_path, llm_factory=failing_factory)
    # Extraction failures are review items, not fetch errors.
    assert result.status == RunStatus.SUCCESS and result.errors == 0
    items = db_session.scalars(select(ReviewItem)).all()
    assert len(items) == 2
    assert {i.reason for i in items} == {ReviewReason.EXTRACTION_FAILED}
    assert all(i.raw_document_id is not None and i.grant_id is None for i in items)
    assert "not a number" in items[0].details["error"]


async def test_without_llm_documents_are_skipped_not_failed(
    db_session: Session, tmp_path: Path
) -> None:
    add_source(db_session, "edctp3")
    await run(db_session, "edctp3", EdctpWeb(), tmp_path)  # no llm_factory
    assert count(db_session, ExtractionResult) == 33
    assert count(db_session, ReviewItem) == 0


async def test_reprocess_extracts_stored_documents(db_session: Session, tmp_path: Path) -> None:
    add_source(db_session, "grants_gov")
    await run(db_session, "grants_gov", GrantsGovWeb(), tmp_path, extract=False)
    assert count(db_session, ExtractionResult) == 0

    source = db_session.scalars(select(Source).where(Source.name == "grants_gov")).one()
    stats = await reprocess_source(
        db_session,
        source,
        storage=LocalRawStorage(tmp_path / "raw"),
        settings=Settings(),
        llm_factory=None,
    )
    assert (stats.documents, stats.grants, stats.failed) == (3, 3, 0)
    assert count(db_session, ExtractionResult) == 3


async def test_unreadable_document_keeps_snapshot(db_session: Session, tmp_path: Path) -> None:
    add_source(db_session, "edctp3")
    web = EdctpWeb()
    web.add(
        "https://www.global-health-edctp3.europa.eu/document/download/"
        "d3d297d7-6fe2-442c-bd25-917f2b6b9dbf_en",
        b"%PDF-1.4 truncated",
        headers={"Content-Type": "application/pdf"},
    )
    client = FakeClient([NOT_A_GRANT])
    result = await run(db_session, "edctp3", web, tmp_path, llm_factory=llm_factory(client))
    assert result.status == RunStatus.SUCCESS
    assert count(db_session, RawDocument) == 35  # the unreadable PDF is still stored
    [item] = db_session.scalars(select(ReviewItem)).all()
    assert item.reason == ReviewReason.EXTRACTION_FAILED
    assert "could not read document" in item.details["error"]
