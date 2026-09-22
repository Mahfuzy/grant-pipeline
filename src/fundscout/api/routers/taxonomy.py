"""GET /taxonomy: slugs and labels for filters and edit forms."""

from fastapi import APIRouter
from sqlalchemy import select

from fundscout.api.deps import SessionDep
from fundscout.pipeline.store import TAXONOMY_MODELS

router = APIRouter(prefix="/taxonomy", tags=["taxonomy"])


@router.get("")
def taxonomy(session: SessionDep) -> dict[str, list[dict[str, str]]]:
    return {
        kind: [
            {"slug": row.slug, "label": row.label}
            for row in session.scalars(select(model).order_by(model.label))
        ]
        for kind, model in TAXONOMY_MODELS.items()
    }
