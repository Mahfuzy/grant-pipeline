from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect

from fundscout.db import migrations
from fundscout.db.models import Base
from tests.conftest import TEST_DATABASE_URL


def test_models_match_migrations(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(ctx, Base.metadata) == []


def test_downgrade_and_upgrade_roundtrip(db_engine: Engine) -> None:
    expected = set(Base.metadata.tables)
    try:
        migrations.downgrade("base", database_url=TEST_DATABASE_URL)
        assert set(inspect(db_engine).get_table_names()) == {"alembic_version"}
    finally:
        migrations.upgrade("head", database_url=TEST_DATABASE_URL)
    assert expected <= set(inspect(db_engine).get_table_names())
