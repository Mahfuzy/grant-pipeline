"""Worker process (SPEC §5 M5): APScheduler runs each source on its cron schedule.

- Only enabled sources with `terms_reviewed` true and a schedule are scheduled (§9); the
  source table is re-read every few minutes, so changes need no restart.
- A source never runs twice at once; missed runs are coalesced into one.
- Repeated failures are alerted in the log (and queued for review) by the runner.
- Grant statuses are recomputed daily as dates pass.
- Cron schedules (`sources.schedule`) are evaluated in UTC.
"""

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from fundscout.config import Settings
from fundscout.db.models import RunStatus, Source

log = structlog.get_logger(__name__)

SYNC_INTERVAL_MINUTES = 5
STATUS_REFRESH_CRON = "10 0 * * *"  # daily, 00:10 UTC
JOB_PREFIX = "source:"

# Runs one source by name; returns the run status (or None if it could not start).
SourceRunner = Callable[[str], Awaitable[RunStatus | None]]


@dataclass
class ScheduledSource:
    name: str
    schedule: str


def schedulable_sources(session: Session) -> tuple[list[ScheduledSource], list[str]]:
    """(sources to schedule, names skipped with the reason logged)."""
    scheduled: list[ScheduledSource] = []
    skipped: list[str] = []
    for source in session.scalars(select(Source).order_by(Source.name)):
        reason = None
        if not source.enabled:
            reason = "disabled"
        elif not source.terms_reviewed:
            reason = "terms not reviewed"
        elif not source.schedule:
            reason = "no schedule"
        if reason:
            skipped.append(source.name)
            log.info("source not scheduled", source=source.name, reason=reason)
        else:
            assert source.schedule
            scheduled.append(ScheduledSource(source.name, source.schedule))
    return scheduled, skipped


@dataclass
class Worker:
    session_factory: sessionmaker[Session]
    run_source: SourceRunner
    refresh_statuses: Callable[[], int]
    scheduler: AsyncIOScheduler = field(default_factory=lambda: AsyncIOScheduler(timezone=UTC))
    _schedules: dict[str, str] = field(default_factory=dict)

    def sync_jobs(self) -> None:
        """Add, update or remove source jobs to match the sources table."""
        with self.session_factory() as session:
            wanted, _ = schedulable_sources(session)
        wanted_by_name = {s.name: s.schedule for s in wanted}

        for name in list(self._schedules):
            if name not in wanted_by_name:
                self.scheduler.remove_job(JOB_PREFIX + name)
                del self._schedules[name]
                log.info("source unscheduled", source=name)

        for name, schedule in wanted_by_name.items():
            if self._schedules.get(name) == schedule:
                continue
            try:
                trigger = CronTrigger.from_crontab(schedule, timezone=UTC)
            except ValueError as exc:
                log.error("invalid schedule", source=name, schedule=schedule, error=str(exc))
                continue
            if name in self._schedules:
                self.scheduler.remove_job(JOB_PREFIX + name)
            self.scheduler.add_job(
                self._run_job,
                trigger,
                args=[name],
                id=JOB_PREFIX + name,
                name=name,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )
            self._schedules[name] = schedule
            log.info("source scheduled", source=name, schedule=schedule)

    async def _run_job(self, name: str) -> None:
        log.info("scheduled run starting", source=name)
        try:
            status = await self.run_source(name)
        except Exception:
            log.exception("scheduled run crashed", source=name)
            return
        log.info("scheduled run finished", source=name, status=status.value if status else None)

    def _refresh_job(self) -> None:
        try:
            self.refresh_statuses()
        except Exception:
            log.exception("status refresh failed")

    def start(self) -> None:
        self.sync_jobs()
        self.scheduler.add_job(
            self.sync_jobs,
            "interval",
            minutes=SYNC_INTERVAL_MINUTES,
            id="sync-sources",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self._refresh_job,
            CronTrigger.from_crontab(STATUS_REFRESH_CRON, timezone=UTC),
            id="refresh-statuses",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()
        log.info("worker started", sources=sorted(self._schedules))

    def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)


def build_worker(settings: Settings) -> Worker:
    from fundscout.db.session import get_sessionmaker, session_scope
    from fundscout.fetch.ratelimit import DomainRateLimiter
    from fundscout.fetch.storage import LocalRawStorage
    from fundscout.pipeline.extraction import make_llm_factory
    from fundscout.pipeline.runner import SourceNotRunnable, build_http_client, run_source
    from fundscout.pipeline.store import refresh_statuses

    factory = get_sessionmaker()
    storage = LocalRawStorage(settings.raw_storage_dir)
    llm_factory = make_llm_factory(settings)
    # Shared across jobs so per-domain politeness holds when sources run concurrently.
    limiter = DomainRateLimiter()

    async def run(name: str) -> RunStatus | None:
        async with build_http_client(settings, rate_limiter=limiter) as http:
            with factory() as session:
                try:
                    result = await run_source(
                        session,
                        name,
                        settings=settings,
                        http=http,
                        storage=storage,
                        llm_factory=llm_factory,
                    )
                except SourceNotRunnable as exc:
                    log.warning("source not runnable", source=name, reason=str(exc))
                    return None
                return result.status

    def refresh() -> int:
        with session_scope() as session:
            return refresh_statuses(session)

    return Worker(factory, run, refresh)


async def run_worker(settings: Settings, stop: asyncio.Event | None = None) -> None:
    if settings.contact_email == "change-me@example.com":
        log.warning("CONTACT_EMAIL is the placeholder; set a real contact address")
    worker = build_worker(settings)
    worker.start()
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):  # non-POSIX
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    log.info("worker stopping")
    worker.shutdown()
