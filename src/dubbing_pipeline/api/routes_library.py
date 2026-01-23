from __future__ import annotations

from contextlib import suppress
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from dubbing_pipeline.api.access import require_job_access, require_library_access
from dubbing_pipeline.api.deps import Identity, require_scope
from dubbing_pipeline.api.middleware import audit_event
from dubbing_pipeline.api.models import Role
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import JobState
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


@router.delete("/{series_slug}/{season_number}/{episode_number}")
async def delete_library_episode(
    request: Request,
    series_slug: str,
    season_number: int,
    episode_number: int,
    ident: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    store = _store(request)
    require_library_access(
        store=store,
        ident=ident,
        series_slug=series_slug,
        season_number=int(season_number),
        episode_number=int(episode_number),
    )
    slug = str(series_slug or "").strip()
    season = int(season_number)
    episode = int(episode_number)
    con = store._conn()
    try:
        params: list[Any] = [slug, season, episode]
        where_owner = ""
        if ident.user.role != Role.admin:
            where_owner = "AND owner_user_id = ?"
            params.append(str(ident.user.id))
        rows = con.execute(
            f"""
            SELECT job_id FROM job_library
            WHERE series_slug = ?
              AND season_number = ?
              AND episode_number = ?
              {where_owner}
            """,
            params,
        ).fetchall()
        job_ids = [str(r["job_id"]) for r in rows]
    finally:
        con.close()
    if not job_ids:
        raise HTTPException(status_code=404, detail="Library item not found")

    q = getattr(request.app.state, "job_queue", None)
    out_root = Path(getattr(request.app.state, "output_root", None) or get_settings().output_dir).resolve()
    deleted: list[str] = []
    for job_id in job_ids:
        job = store.get(job_id)
        if job is None:
            # Clean up dangling library rows.
            with suppress(Exception):
                con = store._conn()
                try:
                    con.execute("DELETE FROM job_library WHERE job_id = ?;", (str(job_id),))
                    con.commit()
                finally:
                    con.close()
            audit_event(
                "library.delete",
                request=request,
                user_id=ident.user.id,
                meta={
                    "series_slug": slug,
                    "season_number": season,
                    "episode_number": episode,
                    "job_id": job_id,
                    "dangling": True,
                },
            )
            continue
        require_job_access(store=store, ident=ident, job=job)
        if q is not None and job.state in {JobState.RUNNING, JobState.QUEUED, JobState.PAUSED}:
            with suppress(Exception):
                await q.kill(job_id, reason="Deleted by user")
        from dubbing_pipeline.ops.retention import delete_job_artifacts

        deleted_ok, _bytes, paths, unsafe = delete_job_artifacts(job=job, output_root=out_root)
        if unsafe:
            raise HTTPException(status_code=400, detail="Refusing to delete outside output dir")
        if not deleted_ok:
            raise HTTPException(status_code=500, detail="Failed to delete job artifacts")
        store.delete_job(job_id)
        deleted.append(job_id)
        audit_event(
            "library.delete",
            request=request,
            user_id=ident.user.id,
            meta={
                "series_slug": slug,
                "season_number": season,
                "episode_number": episode,
                "job_id": job_id,
                "paths": paths,
            },
        )
    return {"ok": True, "deleted_job_ids": deleted}


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

