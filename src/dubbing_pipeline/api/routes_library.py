from __future__ import annotations

from contextlib import suppress
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from dubbing_pipeline.api.access import require_job_access, require_library_access
from dubbing_pipeline.api.deps import Identity, require_scope
from dubbing_pipeline.api.middleware import audit_event
from dubbing_pipeline.api.models import Role
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import JobState, normalize_visibility
from dubbing_pipeline.library import queries
from dubbing_pipeline.library.manifest import update_manifest_visibility
from dubbing_pipeline.library.paths import get_job_output_root, get_library_root_for_job
from dubbing_pipeline.notify import ntfy
from dubbing_pipeline.utils.crypto import random_id
from dubbing_pipeline.utils.log import logger
from dubbing_pipeline.utils.ratelimit import RateLimiter

router = APIRouter(prefix="/api/library", tags=["library"])

_REPORT_REASON_MAX = 200


def _store(request: Request):
    store = getattr(request.app.state, "job_store", None)
    if store is None:
        raise RuntimeError("Job store not initialized")
    return store


def _parse_library_key(key: str) -> tuple[str, int, int]:
    raw = str(key or "").strip()
    parts = raw.split(":")
    if len(parts) != 3:
        raise HTTPException(
            status_code=400, detail="Invalid library key (expected series_slug:season:episode)"
        )
    slug = str(parts[0] or "").strip()
    try:
        season = int(parts[1])
        episode = int(parts[2])
    except Exception:
        raise HTTPException(
            status_code=400, detail="Invalid library key (season/episode must be int)"
        ) from None
    if not slug or season < 1 or episode < 1:
        raise HTTPException(status_code=400, detail="Invalid library key (empty or out of range)")
    return slug, season, episode


def _library_key(series_slug: str, season_number: int, episode_number: int) -> str:
    return f"{series_slug}:{int(season_number)}:{int(episode_number)}"


def _jobs_for_key(store, *, series_slug: str, season_number: int, episode_number: int) -> list[dict[str, str]]:
    slug = str(series_slug or "").strip()
    season = int(season_number)
    episode = int(episode_number)
    con = store._conn()
    try:
        rows = con.execute(
            """
            SELECT job_id, owner_user_id, visibility
            FROM job_library
            WHERE series_slug = ?
              AND season_number = ?
              AND episode_number = ?;
            """,
            (slug, int(season), int(episode)),
        ).fetchall()
        return [
            {
                "job_id": str(r["job_id"] or ""),
                "owner_id": str(r["owner_user_id"] or ""),
                "visibility": str(r["visibility"] or "private"),
            }
            for r in rows
        ]
    finally:
        con.close()


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


@router.post("/{key}/admin_remove")
async def library_admin_remove(
    request: Request,
    key: str,
    ident: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    if ident.user.role != Role.admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    store = _store(request)
    series_slug, season_number, episode_number = _parse_library_key(key)
    rows = _jobs_for_key(
        store, series_slug=series_slug, season_number=season_number, episode_number=episode_number
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Library item not found")
    removed_ids: list[str] = []
    for row in rows:
        job_id = str(row.get("job_id") or "")
        if not job_id:
            continue
        job = store.get(job_id)
        if job is not None:
            store.update(job_id, visibility="private")
            with suppress(Exception):
                update_manifest_visibility(get_library_root_for_job(job) / "manifest.json", "private")
            with suppress(Exception):
                update_manifest_visibility(get_job_output_root(job) / "manifest.json", "private")
        with suppress(Exception):
            con = store._conn()
            try:
                con.execute("DELETE FROM job_library WHERE job_id = ?;", (str(job_id),))
                con.commit()
            finally:
                con.close()
        removed_ids.append(job_id)
    audit_event(
        "library.admin_remove",
        request=request,
        user_id=ident.user.id,
        meta={
            "series_slug": series_slug,
            "season_number": int(season_number),
            "episode_number": int(episode_number),
            "job_ids": removed_ids,
        },
    )
    return {"ok": True, "job_ids": removed_ids, "library_key": _library_key(series_slug, season_number, episode_number)}


@router.post("/{key}/unshare")
async def library_unshare(
    request: Request,
    key: str,
    ident: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    store = _store(request)
    series_slug, season_number, episode_number = _parse_library_key(key)
    require_library_access(
        store=store,
        ident=ident,
        series_slug=series_slug,
        season_number=int(season_number),
        episode_number=int(episode_number),
        allow_shared_read=True,
    )
    rows = _jobs_for_key(
        store, series_slug=series_slug, season_number=season_number, episode_number=episode_number
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Library item not found")
    updated: list[str] = []
    for row in rows:
        job_id = str(row.get("job_id") or "")
        owner_id = str(row.get("owner_id") or "")
        if not job_id:
            continue
        if ident.user.role != Role.admin and owner_id != str(ident.user.id):
            continue
        job = store.get(job_id)
        if job is None:
            continue
        store.update(job_id, visibility="private")
        with suppress(Exception):
            update_manifest_visibility(get_library_root_for_job(job) / "manifest.json", "private")
        with suppress(Exception):
            update_manifest_visibility(get_job_output_root(job) / "manifest.json", "private")
        updated.append(job_id)
    if not updated:
        raise HTTPException(status_code=403, detail="Not permitted to unshare this item")
    audit_event(
        "library.unshare",
        request=request,
        user_id=ident.user.id,
        meta={
            "series_slug": series_slug,
            "season_number": int(season_number),
            "episode_number": int(episode_number),
            "job_ids": updated,
        },
    )
    return {"ok": True, "job_ids": updated, "library_key": _library_key(series_slug, season_number, episode_number)}


@router.post("/{key}/report")
async def library_report(
    request: Request,
    key: str,
    ident: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    store = _store(request)
    series_slug, season_number, episode_number = _parse_library_key(key)
    require_library_access(
        store=store,
        ident=ident,
        series_slug=series_slug,
        season_number=int(season_number),
        episode_number=int(episode_number),
        allow_shared_read=True,
    )
    rl: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if rl is None:
        rl = RateLimiter()
        request.app.state.rate_limiter = rl
    if not rl.allow(f"reports:user:{ident.user.id}", limit=5, per_seconds=3600):
        raise HTTPException(status_code=429, detail="Report rate limit exceeded")
    ip = str(getattr(request.client, "host", "") or "unknown")
    if not rl.allow(f"reports:ip:{ip}", limit=20, per_seconds=3600):
        raise HTTPException(status_code=429, detail="Report rate limit exceeded")

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    reason = str(body.get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")
    if len(reason) > _REPORT_REASON_MAX:
        raise HTTPException(status_code=400, detail="reason too long")

    job_id = queries.latest_episode_job_id(
        store=store,
        ident=ident,
        series_slug=series_slug,
        season_number=int(season_number),
        episode_number=int(episode_number),
    )
    if not job_id:
        raise HTTPException(status_code=404, detail="Library item not found")
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    owner_id = str(getattr(job, "owner_id", "") or "")
    if owner_id == str(ident.user.id):
        raise HTTPException(status_code=400, detail="Use unshare for your own item")
    vis = normalize_visibility(str(getattr(job, "visibility", "") or None)).value
    if vis not in {"shared", "public"}:
        raise HTTPException(status_code=403, detail="Item is not shared")

    report_id = random_id("rpt_", 12)
    notified = False
    notify_error = ""
    s = get_settings()
    admin_topic = str(getattr(s, "ntfy_admin_topic", "") or "").strip()
    if bool(getattr(s, "ntfy_enabled", False)) and admin_topic:
        key_s = _library_key(series_slug, season_number, episode_number)
        msg = (
            f"reporter_id={ident.user.id}\n"
            f"job_id={job_id}\n"
            f"library_key={key_s}\n"
            f"reason={reason}"
        )
        if bool(getattr(s, "report_include_filenames", False)) and str(getattr(job, "video_path", "") or ""):
            with suppress(Exception):
                from pathlib import Path as _Path

                msg += f"\nfile={_Path(str(job.video_path)).name}"
        url = None
        base = str(getattr(s, "public_base_url", "") or "").strip().rstrip("/")
        if base:
            url = (
                f"{base}/ui/library/{series_slug}/season/{int(season_number)}/episode/{int(episode_number)}"
            )
        try:
            notified = bool(
                ntfy.notify(
                    event="library.report",
                    title="Library report",
                    message=msg,
                    url=url,
                    tags=["report"],
                    priority=3,
                    user_id=str(ident.user.id),
                    job_id=str(job_id),
                    topic=admin_topic,
                )
            )
            if not notified:
                notify_error = "ntfy_delivery_failed"
        except Exception as ex:
            notified = False
            notify_error = str(ex)[:120]

    store.create_library_report(
        report_id=report_id,
        reporter_id=str(ident.user.id),
        job_id=str(job_id),
        series_slug=series_slug,
        season_number=int(season_number),
        episode_number=int(episode_number),
        reason=reason,
        owner_id=owner_id,
        notified=bool(notified),
        notify_error=notify_error,
    )
    audit_event(
        "library.report",
        request=request,
        user_id=ident.user.id,
        meta={
            "series_slug": series_slug,
            "season_number": int(season_number),
            "episode_number": int(episode_number),
            "job_id": job_id,
        },
    )
    return {
        "ok": True,
        "report_id": report_id,
        "notified": bool(notified),
        "library_key": _library_key(series_slug, season_number, episode_number),
    }


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

