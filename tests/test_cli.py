import pytest
from typer.testing import CliRunner

from fundscout import __version__
from fundscout.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_config_masks_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:hunter2@db:5432/fundscout")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "hunter2" not in result.stdout
    assert "sk-secret" not in result.stdout
    assert "user:***@db:5432" in result.stdout
