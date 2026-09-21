"""Programmatic access to the Alembic migrations in the repo's `alembic/` directory."""

from pathlib import Path

from alembic import command
from alembic.config import Config

ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


def alembic_config(database_url: str | None = None) -> Config:
    """Alembic config. `database_url` overrides DATABASE_URL from settings."""
    if not ALEMBIC_INI.exists():
        raise FileNotFoundError(f"alembic.ini not found at {ALEMBIC_INI}")
    cfg = Config(str(ALEMBIC_INI))
    if database_url is not None:
        cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def upgrade(revision: str = "head", database_url: str | None = None) -> None:
    command.upgrade(alembic_config(database_url), revision)


def downgrade(revision: str, database_url: str | None = None) -> None:
    command.downgrade(alembic_config(database_url), revision)
