from pathlib import Path

import pytest

from fundscout.config import Settings


def test_defaults() -> None:
    settings = Settings()
    assert settings.default_rate_limit_seconds == 3.0
    assert settings.dedup_match_threshold == 90
    assert settings.dedup_possible_duplicate_threshold == 75
    assert settings.anthropic_api_key is None


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTACT_EMAIL", "ops@fundscout.test")
    monkeypatch.setenv("EXTRACTION_MODEL", "test-model")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("DEDUP_MATCH_THRESHOLD", "85")
    settings = Settings()
    assert settings.extraction_model == "test-model"
    assert settings.dedup_match_threshold == 85
    assert settings.user_agent.endswith("(+mailto:ops@fundscout.test)")
    assert settings.anthropic_api_key is not None
    assert "sk-secret" not in str(settings.model_dump())


def test_env_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("RAW_STORAGE_DIR=/srv/raw\n")
    assert Settings().raw_storage_dir == Path("/srv/raw")


def test_invalid_threshold_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEDUP_MATCH_THRESHOLD", "150")
    with pytest.raises(ValueError):
        Settings()
