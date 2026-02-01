from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from dubbing_pipeline.api.access import require_library_access
from dubbing_pipeline.api.deps import Identity, require_scope
from dubbing_pipeline.library import queries
from dubbing_pipeline.utils.log import logger

from .library_helpers import _store

router = APIRouter()


@router.get("/series")
async def library_series(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    order: str = "title",
    q: str = "",
    view: str = "all",
    ident: Identity = Depends(require_scope("read:job")),
):
    store = _store(request)
    items, meta = queries.list_series(
        store=store, ident=ident, limit=limit, offset=offset, order=order, q=q, view=view
    )
    for it in items:
        require_library_access(
            store=store,
            ident=ident,
            series_slug=str(it.get("series_slug") or ""),
            allow_shared_read=True,
        )
    logger.info(
        "library_series",
        user_id=str(ident.user.id),
        role=str(getattr(ident.user.role, "value", ident.user.role)),
        count=len(items),
        visibility_filter=str(meta.get("visibility_filter")),
        limit=int(meta.get("limit") or limit),
        offset=int(meta.get("offset") or offset),
        order=str(meta.get("order") or order),
        q=str(meta.get("q") or ""),
        view=str(view or "all"),
    )
    return items


@router.get("/recent")
async def library_recent(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    view: str = "all",
    ident: Identity = Depends(require_scope("read:job")),
):
    store = _store(request)
    items, meta = queries.list_recent_episodes(
        store=store, ident=ident, limit=limit, offset=offset, view=view
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
        "library_recent",
        user_id=str(ident.user.id),
        role=str(getattr(ident.user.role, "value", ident.user.role)),
        count=len(items),
        visibility_filter=str(meta.get("visibility_filter")),
        limit=int(meta.get("limit") or limit),
        offset=int(meta.get("offset") or offset),
        view=str(view or "all"),
    )
    return items


@router.get("/continue")
async def library_continue(
    request: Request,
    limit: int = 10,
    ident: Identity = Depends(require_scope("read:job")),
):
    store = _store(request)
    items, meta = queries.list_continue(store=store, ident=ident, user_id=str(ident.user.id), limit=limit)
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
        "library_continue",
        user_id=str(ident.user.id),
        role=str(getattr(ident.user.role, "value", ident.user.role)),
        count=len(items),
        visibility_filter=str(meta.get("visibility_filter")),
        limit=int(meta.get("limit") or limit),
    )
    return items


@router.get("/{series_slug}/seasons")
async def library_seasons(
    request: Request,
    series_slug: str,
    limit: int = 200,
    offset: int = 0,
    view: str = "all",
    ident: Identity = Depends(require_scope("read:job")),
):
    store = _store(request)
    items, meta = queries.list_seasons(
        store=store, ident=ident, series_slug=series_slug, limit=limit, offset=offset, view=view
    )
    require_library_access(
        store=store, ident=ident, series_slug=series_slug, allow_shared_read=True
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
        view=str(view or "all"),
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
    view: str = "all",
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
        view=view,
    )
    require_library_access(
        store=store,
        ident=ident,
        series_slug=series_slug,
        season_number=int(season_number),
        allow_shared_read=True,
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
        view=str(view or "all"),
    )
    return items
