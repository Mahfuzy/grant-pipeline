from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from typer.testing import CliRunner

from fundscout.cli import app
from fundscout.db.session import get_engine
from tests.conftest import TEST_DATABASE_URL

ROOT = Path(__file__).parent.parent.parent
runner = CliRunner()


@pytest.fixture
def cli_db(db_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the CLI at the test database. The CLI commits, so clean up afterwards."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    get_engine.cache_clear()
    yield
    get_engine().dispose()
    get_engine.cache_clear()
    with db_engine.begin() as conn:
        conn.execute(text("DELETE FROM sources WHERE name IN ('grants_gov', 'edctp3')"))


def test_sources_add_and_list(cli_db: None) -> None:
    result = runner.invoke(app, ["sources", "add", "--file", str(ROOT / "sources/edctp3.yaml")])
    assert result.exit_code == 0, result.output
    assert "Added source 'edctp3'" in result.output

    result = runner.invoke(app, ["sources", "add", "--file", str(ROOT / "sources/edctp3.yaml")])
    assert "Updated source 'edctp3'" in result.output

    result = runner.invoke(app, ["sources", "list"])
    assert result.exit_code == 0
    assert "edctp3  [generic_html_listing; enabled, terms reviewed]  last run: never run" in (
        result.output
    )


def test_sources_add_rejects_invalid_config(cli_db: None, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: bad\nadapter: generic_rss\nbase_url: https://x.org\nsource_type: rss\nconfig: {}\n"
    )
    result = runner.invoke(app, ["sources", "add", "--file", str(bad)])
    assert result.exit_code != 0
    assert "feed_url" in str(result.exception)


def test_run_refuses_unknown_source(cli_db: None) -> None:
    result = runner.invoke(app, ["run", "--source", "nope"])
    assert result.exit_code == 1
