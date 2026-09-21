"""Runs one source end to end (SPEC §4). M2 covers discover -> fetch -> store raw -> skip
unchanged; extraction and later stages plug in from M3 onwards.

One failing item never stops the run: the error is logged, recorded in the run's
error_log, and processing continues.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from fundscout.config import Settings
from fundscout.db.models import RawDocument, RunStatus, Source, SourceRun
from fundscout.fetch.http import HttpClient
from fundscout.fetch.ratelimit import DomainRateLimiter
from fundscout.fetch.storage import RawStorage
from fundscout.sources.base import DiscoveredItem, RunContext, SourceAdapter
from fundscout.sources.registry import build_adapter

log = structlog.get_logger(__name__)


class SourceNotRunnable(Exception):
    pass


def build_http_client(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None, **kwargs: Any
) -> HttpClient:
    client = HttpClient(
        user_agent=settings.user_agent,
        timeout=settings.http_timeout_seconds,
        rate_limiter=kwargs.pop("rate_limiter", None) or DomainRateLimiter(),
        transport=transport,
        **kwargs,
    )
    client.default_interval = settings.default_rate_limit_seconds
    return client


MAX_ERROR_LOG_ENTRIES = 200


@dataclass
class Counts:
    items_discovered: int = 0
    items_fetched: int = 0  # network fetches performed
    items_unchanged: int = 0  # items that produced no new snapshot
    errors: int = 0


def check_runnable(source: Source, force: bool) -> None:
    problems = []
    if not source.enabled:
        problems.append("disabled")
    if not source.terms_reviewed:
        problems.append("terms not reviewed")
    if not problems:
        return
    if not force:
        raise SourceNotRunnable(
            f"source {source.name!r} is {' and '.join(problems)}; use --force to run anyway"
        )
    log.warning("running source despite checks", source=source.name, problems=problems)


def latest_snapshot(session: Session, source: Source, url: str) -> RawDocument | None:
    return session.scalars(
        select(RawDocument)
        .where(RawDocument.source_id == source.id, RawDocument.url == url)
        .order_by(RawDocument.fetched_at.desc())
        .limit(1)
    ).first()


async def run_source(
    session: Session,
    source_name: str,
    *,
    settings: Settings,
    http: HttpClient,
    storage: RawStorage,
    force: bool = False,
    limit: int | None = None,
) -> SourceRun:
    source = session.scalars(select(Source).where(Source.name == source_name)).one_or_none()
    if source is None:
        raise SourceNotRunnable(f"no source named {source_name!r}")
    check_runnable(source, force)
    adapter = build_adapter(source.adapter, source.config)

    run = SourceRun(source=source, started_at=datetime.now(UTC), status=RunStatus.RUNNING)
    source.last_run_at = run.started_at
    session.add(run)
    session.commit()

    counts = Counts()
    error_log: list[dict[str, Any]] = []
    with structlog.contextvars.bound_contextvars(source=source.name, run_id=str(run.id)):
        ctx = RunContext(
            source=source, run_id=run.id, http=http, settings=settings, limit=limit, log=log
        )
        log.info("run started", adapter=source.adapter, force=force, limit=limit)
        try:
            items = await adapter.discover(source, ctx)
        except Exception as exc:
            log.exception("discovery failed")
            error_log.append({"stage": "discover", "error": repr(exc)})
            counts.errors += 1
            return _finish(session, run, source, counts, error_log, discovery_failed=True)

        counts.items_discovered = len(items)
        _save_progress(session, run, counts, error_log)
        log.info("discovered items", count=len(items))

        for item in items:
            with structlog.contextvars.bound_contextvars(url=item.url):
                try:
                    await _process_item(session, adapter, item, ctx, storage, counts)
                    session.commit()
                except Exception as exc:
                    session.rollback()
                    log.error("item failed", error=repr(exc))
                    counts.errors += 1
                    if len(error_log) < MAX_ERROR_LOG_ENTRIES:
                        error_log.append({"stage": "fetch", "url": item.url, "error": repr(exc)})
                _save_progress(session, run, counts, error_log)

        return _finish(session, run, source, counts, error_log)


async def _process_item(
    session: Session,
    adapter: SourceAdapter[Any],
    item: DiscoveredItem,
    ctx: RunContext,
    storage: RawStorage,
    counts: Counts,
) -> None:
    now = datetime.now(UTC)
    previous = latest_snapshot(session, ctx.source, item.url)

    # Skip the fetch when discovery data is unchanged and the content was verified
    # recently. A skip does not count as verification, so last_checked_at is left alone
    # and the document is re-fetched once the window expires.
    refetch_days = adapter.config.refetch_unchanged_after_days
    if (
        previous is not None
        and refetch_days is not None
        and item.change_key is not None
        and previous.change_key == item.change_key
        and previous.last_checked_at > now - timedelta(days=refetch_days)
    ):
        counts.items_unchanged += 1
        log.info("unchanged", reason="discovery_unchanged")
        return

    document = await adapter.fetch(item, ctx, previous)
    if item.inline_content is None:
        counts.items_fetched += 1

    if document.not_modified and previous is not None:
        previous.last_checked_at = now
        previous.change_key = item.change_key
        counts.items_unchanged += 1
        log.info("unchanged", reason="not_modified")
        return

    digest = adapter.content_hash(document)
    if previous is not None and previous.content_hash == digest:
        previous.last_checked_at = now
        previous.change_key = item.change_key
        previous.etag = document.etag or previous.etag
        previous.last_modified = document.last_modified or previous.last_modified
        counts.items_unchanged += 1
        log.info("unchanged", reason="same_hash")
        return

    key = storage.save(document.content)
    session.add(
        RawDocument(
            source_id=ctx.source.id,
            url=item.url,
            fetched_at=document.fetched_at,
            http_status=document.status_code,
            content_type=document.content_type,
            content_hash=digest,
            storage_path=key,
            etag=document.etag,
            last_modified=document.last_modified,
            last_checked_at=now,
            change_key=item.change_key,
        )
    )
    log.info("stored snapshot", new=previous is None, storage_path=key)


def _save_progress(
    session: Session, run: SourceRun, counts: Counts, error_log: list[dict[str, Any]]
) -> None:
    run.items_discovered = counts.items_discovered
    run.items_fetched = counts.items_fetched
    run.items_unchanged = counts.items_unchanged
    run.errors = counts.errors
    run.error_log = list(error_log)
    session.commit()


def _finish(
    session: Session,
    run: SourceRun,
    source: Source,
    counts: Counts,
    error_log: list[dict[str, Any]],
    discovery_failed: bool = False,
) -> SourceRun:
    run.finished_at = datetime.now(UTC)
    if discovery_failed or (counts.errors and counts.errors >= counts.items_discovered):
        run.status = RunStatus.FAILED
    elif counts.errors:
        run.status = RunStatus.PARTIAL
    else:
        run.status = RunStatus.SUCCESS
    # A partial run still completed discovery and checked most items.
    if run.status in (RunStatus.SUCCESS, RunStatus.PARTIAL):
        source.last_success_at = run.finished_at
    _save_progress(session, run, counts, error_log)
    log.info(
        "run finished",
        status=run.status.value,
        discovered=counts.items_discovered,
        fetched=counts.items_fetched,
        unchanged=counts.items_unchanged,
        errors=counts.errors,
    )
    return run
