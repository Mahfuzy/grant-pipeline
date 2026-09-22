"""Worker scheduling (M5): which sources are scheduled, and the jobs it creates."""

import asyncio

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import Connection
from sqlalchemy.orm import Session, sessionmaker

from fundscout.db.models import RunStatus, Source, SourceType
from fundscout.pipeline.scheduler import JOB_PREFIX, Worker


def add(session: Session, name: str, **fields: object) -> Source:
    values: dict[str, object] = {
        "adapter": "generic_rss",
        "base_url": "https://example.org",
        "source_type": SourceType.RSS,
        "config": {"feed_url": "https://example.org/feed"},
        "schedule": "0 5 * * *",
        "enabled": True,
        "terms_reviewed": True,
    }
    values.update(fields)
    source = Source(name=name, **values)
    session.add(source)
    session.flush()
    return source


def make_worker(db_connection: Connection, ran: list[str]) -> Worker:
    factory = sessionmaker(bind=db_connection, join_transaction_mode="create_savepoint")

    async def run(name: str) -> RunStatus | None:
        ran.append(name)
        return RunStatus.SUCCESS

    return Worker(factory, run, lambda: 0)


def job_names(worker: Worker) -> set[str]:
    return {job.id.removeprefix(JOB_PREFIX) for job in worker.scheduler.get_jobs()}


def test_only_reviewed_enabled_scheduled_sources_get_jobs(
    db_session: Session, db_connection: Connection
) -> None:
    add(db_session, "ok")
    add(db_session, "unreviewed", terms_reviewed=False)
    add(db_session, "disabled", enabled=False)
    add(db_session, "manual_only", schedule=None)
    add(db_session, "bad_cron", schedule="every day")
    db_session.commit()

    worker = make_worker(db_connection, [])
    worker.sync_jobs()
    assert job_names(worker) == {"ok"}
    job = worker.scheduler.get_job(JOB_PREFIX + "ok")
    assert isinstance(job.trigger, CronTrigger)
    assert job.max_instances == 1 and job.coalesce


def test_sync_follows_source_changes(db_session: Session, db_connection: Connection) -> None:
    source = add(db_session, "ok")
    db_session.commit()
    worker = make_worker(db_connection, [])
    worker.sync_jobs()

    source.schedule = "30 6 * * 1"
    db_session.commit()
    worker.sync_jobs()
    assert "day_of_week='1'" in str(worker.scheduler.get_job(JOB_PREFIX + "ok").trigger)

    source.terms_reviewed = False
    db_session.commit()
    worker.sync_jobs()
    assert job_names(worker) == set()


def test_job_runs_the_source_and_survives_crashes(
    db_session: Session, db_connection: Connection
) -> None:
    ran: list[str] = []
    worker = make_worker(db_connection, ran)
    asyncio.run(worker._run_job("ok"))
    assert ran == ["ok"]

    async def crash(name: str) -> RunStatus | None:
        raise RuntimeError("boom")

    worker.run_source = crash
    asyncio.run(worker._run_job("ok"))  # logged, not raised


async def test_worker_starts_and_stops(db_session: Session, db_connection: Connection) -> None:
    add(db_session, "ok")
    db_session.commit()
    worker = make_worker(db_connection, [])
    worker.start()
    try:
        ids = {job.id for job in worker.scheduler.get_jobs()}
        assert ids == {JOB_PREFIX + "ok", "sync-sources", "refresh-statuses"}
    finally:
        worker.shutdown()
