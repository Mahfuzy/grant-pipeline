"""GET /sources, GET /sources/{id}/runs, POST /sources/{id}/run (SPEC §11)."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from fundscout.api.deps import SessionDep, SettingsDep
from fundscout.api.schemas import RunOut, RunRequested, SourceOut
from fundscout.api.serialise import run_out, source_out
from fundscout.config import Settings
from fundscout.db.models import GrantSource, RunStatus, Source, SourceRun

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/sources", tags=["sources"])

# A run still marked "running" after this long is assumed to have died.
STALE_RUN = timedelta(hours=6)

SourceRunner = Callable[[str, bool, Settings], Awaitable[None]]


async def run_in_background(name: str, force: bool, settings: Settings) -> None:
    from fundscout.db.session import get_sessionmaker
    from fundscout.fetch.storage import LocalRawStorage
    from fundscout.pipeline.extraction import make_llm_factory
    from fundscout.pipeline.runner import build_http_client, run_source

    try:
        async with build_http_client(settings) as http:
            with get_sessionmaker()() as session:
                await run_source(
                    session,
                    name,
                    settings=settings,
                    http=http,
                    storage=LocalRawStorage(settings.raw_storage_dir),
                    force=force,
                    llm_factory=make_llm_factory(settings),
                )
    except Exception:
        log.exception("manual run failed", source=name)


def source_runner() -> SourceRunner:
    return run_in_background


@router.get("", response_model=list[SourceOut])
def list_sources(session: SessionDep) -> list[SourceOut]:
    counts: dict[uuid.UUID, int] = {
        source_id: grants
        for source_id, grants in session.execute(
            select(GrantSource.source_id, func.count(func.distinct(GrantSource.grant_id))).group_by(
                GrantSource.source_id
            )
        ).all()
    }
    sources = session.scalars(select(Source).order_by(Source.name)).all()
    return [source_out(session, s, counts.get(s.id, 0)) for s in sources]


def _get(session: SessionDep, source_id: uuid.UUID) -> Source:
    source = session.get(Source, source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found")
    return source


@router.get("/{source_id}/runs", response_model=list[RunOut])
def list_runs(
    source_id: uuid.UUID, session: SessionDep, limit: int = Query(50, ge=1, le=500)
) -> list[RunOut]:
    _get(session, source_id)
    runs = session.scalars(
        select(SourceRun)
        .where(SourceRun.source_id == source_id)
        .order_by(SourceRun.started_at.desc())
        .limit(limit)
    ).all()
    return [run_out(r) for r in runs]


@router.post("/{source_id}/run", response_model=RunRequested, status_code=status.HTTP_202_ACCEPTED)
def trigger_run(
    source_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    background: BackgroundTasks,
    runner: Annotated[SourceRunner, Depends(source_runner)],
    force: bool = Query(False, description="Run even if disabled or terms not reviewed"),
) -> RunRequested:
    """Start a run in the background; poll GET /sources/{id}/runs for the result."""
    source = _get(session, source_id)
    if not force and not (source.enabled and source.terms_reviewed):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "source is disabled or its terms have not been reviewed (use force=true)",
        )
    running = session.scalars(
        select(SourceRun).where(
            SourceRun.source_id == source.id,
            SourceRun.status == RunStatus.RUNNING,
            SourceRun.started_at > datetime.now(UTC) - STALE_RUN,
        )
    ).first()
    if running is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "a run of this source is in progress")
    if force:
        log.warning("manual run forced", source=source.name)
    background.add_task(runner, source.name, force, settings)
    return RunRequested(source=source.name, message="run started")
