"""Run extraction for stored raw documents and record the outcome.

Success: one `extraction_results` row (all grants found in the document, their issues,
token usage). Failure: an `extraction_failed` review item. LLM extraction without a
configured model/credentials is skipped and only logged, so an unconfigured machine does
not flood the review queue.
"""

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic
import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fundscout.config import Settings
from fundscout.db.models import (
    ExtractionResult,
    RawDocument,
    ReviewItem,
    ReviewReason,
    Source,
)
from fundscout.extract.base import ExtractionFailed, ExtractionInput
from fundscout.extract.groq import GroqClient
from fundscout.extract.llm import LlmExtractor, LlmNotConfigured
from fundscout.extract.pipeline import (
    ExtractionConfig,
    LlmFactory,
    LlmOptions,
    Skipped,
    extract_document,
)
from fundscout.extract.schema import CandidateGrant
from fundscout.fetch.storage import RawStorage
from fundscout.pipeline.store import StoreStats, store_grants
from fundscout.sources.registry import get_adapter_class

log = structlog.get_logger(__name__)

_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "calls",
)


@dataclass
class ExtractionStats:
    documents: int = 0
    grants: int = 0
    failed: int = 0
    skipped: int = 0
    llm_not_configured: int = 0
    store_failed: int = 0
    store: StoreStats = field(default_factory=StoreStats)
    usage: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_USAGE_KEYS, 0))

    def add_usage(self, usage: dict[str, int]) -> None:
        for key in _USAGE_KEYS:
            self.usage[key] += int(usage.get(key, 0) or 0)

    def as_log(self) -> dict[str, Any]:
        return {
            "extracted_documents": self.documents,
            "extracted_grants": self.grants,
            "extraction_failed": self.failed,
            "extraction_skipped": self.skipped + self.llm_not_configured,
            "grants_created": self.store.created,
            "grants_updated": self.store.updated,
            "grants_unchanged": self.store.unchanged,
            "store_failed": self.store_failed,
            **{f"llm_{k}": v for k, v in self.usage.items()},
        }


def make_llm_factory(settings: Settings, client: Any = None) -> LlmFactory | None:
    """LLM extractor factory from settings, or None when no model is configured.

    Anthropic credentials come from ANTHROPIC_API_KEY when set, otherwise from the SDK's
    own resolution (e.g. an `ant auth login` profile). Groq needs GROQ_API_KEY.
    """
    if not settings.extraction_model:
        return None
    model = settings.extraction_model
    cached: list[Any] = [client]

    def factory(
        options: LlmOptions, validate: Callable[[list[CandidateGrant]], None]
    ) -> LlmExtractor:
        if cached[0] is None:
            cached[0] = _make_client(settings)
        return LlmExtractor(
            cached[0],
            model,
            validate=validate,
            max_input_chars=options.max_input_chars,
            content_selector=options.content_selector,
        )

    return factory


def _make_client(settings: Settings) -> Any:
    if settings.llm_provider == "groq":
        if settings.groq_api_key is None:
            raise LlmNotConfigured("LLM_PROVIDER is groq but GROQ_API_KEY is not set")
        return GroqClient(
            settings.groq_api_key.get_secret_value(),
            base_url=settings.groq_base_url,
            strict=settings.groq_strict_output,
        )
    key = settings.anthropic_api_key
    try:
        return anthropic.Anthropic(api_key=key.get_secret_value()) if key else anthropic.Anthropic()
    except anthropic.AnthropicError as exc:
        raise LlmNotConfigured(f"no Anthropic credentials: {exc}") from exc


def extraction_config(source: Source) -> ExtractionConfig:
    config = get_adapter_class(source.adapter).validate_config(source.config)
    return config.extraction


async def extract_raw_document(
    session: Session,
    source: Source,
    raw: RawDocument,
    content: bytes,
    *,
    settings: Settings,
    llm_factory: LlmFactory | None,
    stats: ExtractionStats,
    run_id: uuid.UUID | None = None,
    store: bool = True,
) -> ExtractionResult | None:
    """Extract one stored document, persist the result (or a review item) and, unless
    store=False, upsert the grants it describes."""
    doc = ExtractionInput(
        url=raw.url,
        content=content,
        content_type=raw.content_type,
        metadata=raw.discovery_metadata or {},
    )
    config = extraction_config(source)
    try:
        extraction = await asyncio.to_thread(
            extract_document,
            doc,
            config,
            llm_factory=llm_factory,
            low_confidence_threshold=settings.review_low_confidence_threshold,
        )
    except Skipped:
        stats.skipped += 1
        return None
    except LlmNotConfigured as exc:
        stats.llm_not_configured += 1
        if stats.llm_not_configured == 1:
            log.warning("LLM extraction not configured; skipping LLM documents", reason=str(exc))
        return None
    except Exception as exc:
        # Includes unexpected errors (e.g. a corrupt PDF): the stored snapshot is kept and
        # the failure goes to the review queue instead of failing the fetch.
        failure = exc if isinstance(exc, ExtractionFailed) else ExtractionFailed(repr(exc))
        stats.failed += 1
        stats.add_usage(failure.details.get("usage") or {})
        log.error("extraction failed", error=str(failure), exc_info=failure is not exc)
        session.add(
            ReviewItem(
                raw_document_id=raw.id,
                reason=ReviewReason.EXTRACTION_FAILED,
                details={"url": raw.url, "error": str(failure)[:2000], **failure.details},
            )
        )
        return None

    output = extraction.output
    stats.documents += 1
    stats.grants += len(extraction.results)
    stats.add_usage(output.usage)
    result = ExtractionResult(
        raw_document_id=raw.id,
        method=output.method,
        model=output.model,
        output={
            "grants": [r.grant.model_dump(mode="json") for r in extraction.results],
            "issues": [[i.as_dict() for i in r.issues] for r in extraction.results],
            "notes": output.notes,
            "usage": output.usage,
        },
        field_confidence=[r.grant.field_confidence for r in extraction.results],
    )
    session.add(result)
    log.info(
        "extracted",
        method=output.method.value,
        grants=len(extraction.results),
        issues=sum(len(r.issues) for r in extraction.results),
    )
    if store:
        try:
            # Savepoint: a storage bug loses this document's grants, not the snapshot
            # and extraction result (which reprocess can pick up later).
            with session.begin_nested():
                stats.store.add(
                    store_grants(
                        session, source, raw, extraction.results, settings=settings, run_id=run_id
                    )
                )
        except Exception:
            stats.store_failed += 1
            log.exception("storing grants failed")
    return result


async def reprocess_source(
    session: Session,
    source: Source,
    *,
    storage: RawStorage,
    settings: Settings,
    llm_factory: LlmFactory | None,
    limit: int | None = None,
) -> ExtractionStats:
    """Re-extract the latest stored snapshot of every document of a source (no fetching)."""
    latest = (
        select(RawDocument.url, func.max(RawDocument.fetched_at).label("fetched_at"))
        .where(RawDocument.source_id == source.id)
        .group_by(RawDocument.url)
        .subquery()
    )
    query = (
        select(RawDocument)
        .join(
            latest,
            (RawDocument.url == latest.c.url) & (RawDocument.fetched_at == latest.c.fetched_at),
        )
        .where(RawDocument.source_id == source.id)
        .order_by(RawDocument.url)
    )
    if limit is not None:
        query = query.limit(limit)
    stats = ExtractionStats()
    with structlog.contextvars.bound_contextvars(source=source.name, mode="reprocess"):
        for raw in session.scalars(query).all():
            with structlog.contextvars.bound_contextvars(url=raw.url):
                try:
                    content = storage.load(raw.storage_path)
                    await extract_raw_document(
                        session,
                        source,
                        raw,
                        content,
                        settings=settings,
                        llm_factory=llm_factory,
                        stats=stats,
                    )
                    session.commit()
                except Exception as exc:
                    session.rollback()
                    stats.failed += 1
                    log.error("reprocess failed", error=repr(exc))
        log.info("reprocess finished", **stats.as_log())
    return stats
