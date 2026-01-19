from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from dubbing_pipeline.api.deps import Identity, require_scope
from dubbing_pipeline.api.routes_settings import UserSettingsStore
from dubbing_pipeline.api.security import verify_csrf
from dubbing_pipeline.library import queries
from dubbing_pipeline.utils.log import logger

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
    q: str = "",
    status: str = "",
    view: str = "all",
    ident: Identity = Depends(require_scope("read:job")),
):
    store = _store(request)
    items, meta = queries.list_series(
        store=store,
        ident=ident,
        limit=limit,
        offset=offset,
        order=order,
        q=q,
        status=status,
        view=view,
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
        status=str(meta.get("status_filter") or ""),
        view=str(view or "all"),
    )
    return items


@router.get("/{series_slug}/seasons")
async def library_seasons(
    request: Request,
    series_slug: str,
    limit: int = 200,
    offset: int = 0,
    status: str = "",
    view: str = "all",
    ident: Identity = Depends(require_scope("read:job")),
):
    store = _store(request)
    items, meta = queries.list_seasons(
        store=store,
        ident=ident,
        series_slug=series_slug,
        limit=limit,
        offset=offset,
        status=status,
        view=view,
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
        status=str(meta.get("status_filter") or ""),
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
    status: str = "",
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
        status=status,
        view=view,
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
        status=str(meta.get("status_filter") or ""),
        view=str(view or "all"),
    )
    return items


@router.get("/recent")
async def library_recent(
    request: Request,
    limit: int = 12,
    offset: int = 0,
    status: str = "has_outputs",
    view: str = "all",
    ident: Identity = Depends(require_scope("read:job")),
):
    store = _store(request)
    items, meta = queries.list_recent(
        store=store, ident=ident, limit=limit, offset=offset, status=status, view=view
    )
    logger.info(
        "library_recent",
        user_id=str(ident.user.id),
        role=str(getattr(ident.user.role, "value", ident.user.role)),
        count=len(items),
        visibility_filter=str(meta.get("visibility_filter")),
        limit=int(meta.get("limit") or limit),
        offset=int(meta.get("offset") or offset),
        status=str(meta.get("status_filter") or ""),
        view=str(view or "all"),
    )
    return {"items": items, "meta": meta}


@router.get("/search")
async def library_search(
    request: Request,
    q: str = "",
    season_number: int | None = None,
    episode_number: int | None = None,
    status: str = "",
    limit: int = 50,
    offset: int = 0,
    view: str = "all",
    ident: Identity = Depends(require_scope("read:job")),
):
    store = _store(request)
    items, meta = queries.search_library(
        store=store,
        ident=ident,
        q=q,
        season_number=season_number,
        episode_number=episode_number,
        status=status,
        limit=limit,
        offset=offset,
        view=view,
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
        status=str(meta.get("status_filter") or ""),
        season_number=str(meta.get("season_number") or ""),
        episode_number=str(meta.get("episode_number") or ""),
        view=str(view or "all"),
    )
    return {"items": items, "meta": meta}


def _settings_store(request: Request) -> UserSettingsStore:
    st = getattr(request.app.state, "user_settings_store", None)
    if isinstance(st, UserSettingsStore):
        return st
    st = UserSettingsStore()
    request.app.state.user_settings_store = st
    return st


@router.get("/continue")
async def library_continue(
    request: Request,
    fallback: int = 1,
    ident: Identity = Depends(require_scope("read:job")),
):
    store = _settings_store(request)
    cfg = store.get_user(ident.user.id)
    lib = cfg.get("library") if isinstance(cfg.get("library"), dict) else {}
    last = lib.get("last_series") if isinstance(lib, dict) else None
    if isinstance(last, dict) and str(last.get("series_slug") or "").strip():
        return {"item": last}
    if not int(fallback or 0):
        return {"item": None}
    items, _ = queries.list_series(store=_store(request), ident=ident, limit=1, order="recent")
    if items:
        item = {
            "series_slug": str(items[0].get("series_slug") or ""),
            "series_title": str(items[0].get("series_title") or ""),
            "updated_at": str(items[0].get("latest_updated_at") or ""),
        }
        return {"item": item}
    return {"item": None}


@router.post("/continue")
async def library_continue_update(
    request: Request,
    ident: Identity = Depends(require_scope("read:job")),
):
    verify_csrf(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    slug = str(body.get("series_slug") or "").strip()
    title = str(body.get("series_title") or "").strip()
    store = _settings_store(request)
    patch = {"library": {"last_series": {"series_slug": slug, "series_title": title}}}
    updated = store.update_user(ident.user.id, patch)
    lib = updated.get("library") if isinstance(updated.get("library"), dict) else {}
    last = lib.get("last_series") if isinstance(lib, dict) else None
    return {"ok": True, "item": last or None}

