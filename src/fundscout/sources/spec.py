"""Source definitions loaded from YAML (`fundscout sources add --file`)."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from fundscout.db.models import Source, SourceType
from fundscout.sources.registry import get_adapter_class


class SourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=200)
    adapter: str
    base_url: str
    source_type: SourceType
    schedule: str | None = None  # cron expression, used by the worker (M5)
    enabled: bool = True
    terms_reviewed: bool = False
    notes: str | None = None
    config: dict[str, Any] = {}

    @model_validator(mode="after")
    def _validate_adapter_config(self) -> "SourceSpec":
        get_adapter_class(self.adapter).validate_config(self.config)
        return self


def load_source_spec(path: Path) -> SourceSpec:
    return SourceSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def upsert_source(session: Session, spec: SourceSpec) -> tuple[Source, bool]:
    """Create the source, or update it in place if one with this name exists."""
    source = session.scalars(select(Source).where(Source.name == spec.name)).one_or_none()
    created = source is None
    if source is None:
        source = Source(name=spec.name)
        session.add(source)
    for field, value in spec.model_dump(exclude={"name"}).items():
        setattr(source, field, value)
    session.flush()
    return source, created
