import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, create_engine, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from fundscout.config import Settings, get_settings
from fundscout.db import migrations

# Read before the autouse fixture below clears settings env vars.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://fundscout:fundscout@localhost:5432/fundscout_test"
)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep tests independent of the developer's shell env and local `.env`."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()


def _ensure_database(url: str) -> None:
    """Create the test database if it doesn't exist."""
    target = make_url(url)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": target.database}
            )
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    """Engine for a freshly migrated test database. Skips if Postgres is unreachable."""
    database = make_url(TEST_DATABASE_URL).database or ""
    if not database.endswith("_test"):
        pytest.exit(f"Refusing to wipe {database!r}: TEST_DATABASE_URL must name a *_test database")
    try:
        _ensure_database(TEST_DATABASE_URL)
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable at {TEST_DATABASE_URL} ({exc.orig})")
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    migrations.upgrade("head", database_url=TEST_DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def db_connection(db_engine: Engine) -> Iterator[Connection]:
    with db_engine.connect() as conn:
        trans = conn.begin()
        yield conn
        trans.rollback()


@pytest.fixture
def db_session(db_connection: Connection) -> Iterator[Session]:
    """Session whose work is rolled back after the test (commits become savepoints)."""
    session = Session(bind=db_connection, join_transaction_mode="create_savepoint")
    yield session
    session.close()
