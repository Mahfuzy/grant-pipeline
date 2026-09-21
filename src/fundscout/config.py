"""Application settings, loaded from environment variables and `.env`."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str = "postgresql+psycopg://fundscout:fundscout@localhost:5432/fundscout"

    # LLM extraction
    anthropic_api_key: SecretStr | None = None
    extraction_model: str | None = None

    # Responsible collection (SPEC §9)
    contact_email: str = "change-me@example.com"
    default_rate_limit_seconds: float = Field(default=3.0, gt=0)
    http_timeout_seconds: float = Field(default=30.0, gt=0)

    # Raw snapshot storage
    raw_storage_dir: Path = Path("data/raw")

    # Dedup thresholds (SPEC §8.1), rapidfuzz token_set_ratio scores 0-100
    dedup_match_threshold: float = Field(default=90, ge=0, le=100)
    dedup_possible_duplicate_threshold: float = Field(default=75, ge=0, le=100)

    # Review rules (SPEC §6.3, §8.3)
    review_low_confidence_threshold: float = Field(default=0.6, ge=0, le=1)
    missing_runs_before_review: int = Field(default=3, ge=1)
    fetch_failures_before_review: int = Field(default=3, ge=1)

    # Internal API
    api_key: SecretStr | None = None

    # Logging
    log_level: str = "INFO"
    log_json: bool = True

    @property
    def user_agent(self) -> str:
        from fundscout import __version__

        return f"FundscoutBot/{__version__} (+mailto:{self.contact_email})"


@lru_cache
def get_settings() -> Settings:
    return Settings()
