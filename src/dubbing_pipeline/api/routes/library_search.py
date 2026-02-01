from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from dubbing_pipeline.api.access import require_library_access
from dubbing_pipeline.api.deps import Identity, require_scope
from dubbing_pipeline.library import queries
from dubbing_pipeline.utils.log import logger

from .library_helpers import _store

router = APIRouter()


@router.get("/search")
async def library_search(
    request: Request,
    q: str,
    limit: int = 50,
    offset: int = 0,
    view: str = "all",
    ident: Identity = Depends(require_scope("read:job")),
):
    store = _store(request)
    items, meta = queries.search_library(
        store=store, ident=ident, q=q, limit=limit, offset=offset, view=view
    )
    for it in items:
        require_library_access(
            store=store,
            ident=ident,
            series_slug=str(it.get("series_slug") or ""),
            season_number=int(it.get("season_number") or 0),
            episode_number=int(it.get("episode_number") or 0),
            allow_shared_read=True,
        )
    logger.info(
        "library_search",
        user_id=str(ident.user.id),
        role=str(getattr(ident.user.role, "value", ident.user.role)),
        count=len(items),
        visibility_filter=str(meta.get("visibility_filter")),
        limit=int(meta.get("limit") or limit),
        offset=int(meta.get("offset") or offset),
        q=str(meta.get("q") or ""),
        view=str(view or "all"),
    )
    return items
