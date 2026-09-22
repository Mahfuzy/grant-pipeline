"""Shared API dependencies: database session and API-key auth."""

import secrets
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from fundscout.config import Settings, get_settings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_session() -> Iterator[Session]:
    from fundscout.db.session import get_sessionmaker

    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def settings_dep() -> Settings:
    return get_settings()


def require_api_key(
    key: Annotated[str | None, Security(_api_key_header)],
    settings: Annotated[Settings, Depends(settings_dep)],
) -> None:
    """Every endpoint except /health needs `X-API-Key` to equal API_KEY. Without an
    API_KEY configured the API refuses rather than running open."""
    if settings.api_key is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API_KEY is not configured")
    expected = settings.api_key.get_secret_value().encode()
    if key is None or not secrets.compare_digest(key.encode(), expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing X-API-Key")


SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(settings_dep)]
