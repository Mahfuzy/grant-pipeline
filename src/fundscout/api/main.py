"""Internal API (SPEC §11). All endpoints except /health need the X-API-Key header.

Run with `fundscout api` (uvicorn). The built admin UI is served at /admin when present.
"""

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Response, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from fundscout import __version__
from fundscout.api.deps import SessionDep, require_api_key
from fundscout.api.routers import changes, grants, review, sources, taxonomy
from fundscout.config import get_settings


def create_app(admin_ui_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="Fundscout internal API", version=__version__)
    auth = [Depends(require_api_key)]
    for module in (grants, changes, sources, review, taxonomy):
        app.include_router(module.router, dependencies=auth)

    @app.get("/health", tags=["health"])
    def health(session: SessionDep, response: Response) -> dict[str, Any]:
        try:
            session.execute(text("SELECT 1"))
            database = "ok"
        except Exception:
            database = "unavailable"
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ok" if database == "ok" else "degraded",
            "database": database,
            "version": __version__,
        }

    ui_dir = admin_ui_dir if admin_ui_dir is not None else get_settings().admin_ui_dir
    if ui_dir.is_dir():
        app.mount("/admin", StaticFiles(directory=ui_dir, html=True), name="admin")

        @app.get("/", include_in_schema=False)
        def root() -> RedirectResponse:
            return RedirectResponse("/admin/")

    return app


app = create_app()
