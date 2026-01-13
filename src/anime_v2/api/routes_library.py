from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from anime_v2.api.deps import Identity, require_scope
from anime_v2.library import queries
from anime_v2.utils.log import logger

router = APIRouter(prefix="/api/library", tags=["library"])


def _store(request: Request):
    store = getattr(request.app.state, "job_store", None)
    if store is None:
        raise RuntimeError("Job store not initialized")
    return store


@router.get("/series")
async def library_series(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    order: str = "title",
    ident: Identity = Depends(require_scope("read:job")),
):
    store = _store(request)
    items, meta = queries.list_series(store=store, ident=ident, limit=limit, offset=offset, order=order)
    logger.info(
        "library_series",
        user_id=str(ident.user.id),
        role=str(getattr(ident.user.role, "value", ident.user.role)),
        count=len(items),
        visibility_filter=str(meta.get("visibility_filter")),
        limit=int(meta.get("limit") or limit),
        offset=int(meta.get("offset") or offset),
        order=str(meta.get("order") or order),
    )
    return items


@router.get("/{series_slug}/seasons")
async def library_seasons(
    request: Request,
    series_slug: str,
    limit: int = 200,
    offset: int = 0,
    ident: Identity = Depends(require_scope("read:job")),
):
    store = _store(request)
    items, meta = queries.list_seasons(
        store=store, ident=ident, series_slug=series_slug, limit=limit, offset=offset
    )
    logger.info(
        "library_seasons",
        user_id=str(ident.user.id),
        role=str(getattr(ident.user.role, "value", ident.user.role)),
        series_slug=str(series_slug),
        count=len(items),
        visibility_filter=str(meta.get("visibility_filter")),
        limit=int(meta.get("limit") or limit),
        offset=int(meta.get("offset") or offset),
    )
    return items


@router.get("/{series_slug}/{season_number}/episodes")
async def library_episodes(
    request: Request,
    series_slug: str,
    season_number: int,
    limit: int = 200,
    offset: int = 0,
    episode_number: int | None = None,
    include_versions: int = 0,
    ident: Identity = Depends(require_scope("read:job")),
):
    store = _store(request)
    items, meta = queries.list_episodes(
        store=store,
        ident=ident,
        series_slug=series_slug,
        season_number=int(season_number),
        limit=limit,
        offset=offset,
        episode_number=episode_number,
        include_versions=bool(int(include_versions or 0)),
    )
    logger.info(
        "library_episodes",
        user_id=str(ident.user.id),
        role=str(getattr(ident.user.role, "value", ident.user.role)),
        series_slug=str(series_slug),
        season_number=int(season_number),
        count=len(items),
        visibility_filter=str(meta.get("visibility_filter")),
        include_versions=bool(meta.get("include_versions")),
        episode_number=meta.get("episode_number"),
        limit=int(meta.get("limit") or limit),
        offset=int(meta.get("offset") or offset),
    )
    return items

