from __future__ import annotations

import time
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from dubbing_pipeline.api.access import require_job_access
from dubbing_pipeline.api.deps import Identity, require_scope
from dubbing_pipeline.api.middleware import audit_event
from dubbing_pipeline.jobs.models import JobState, normalize_visibility
from dubbing_pipeline.library.manifest import update_manifest_visibility
from dubbing_pipeline.library.paths import get_job_output_root, get_library_root_for_job
from dubbing_pipeline.runtime.scheduler import JobRecord
from dubbing_pipeline.web.routes.jobs_common import _get_queue, _get_scheduler, _get_store, _output_root

router = APIRouter()


@router.delete("/api/jobs/{id}")
async def delete_job(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    # Best-effort cancel first
    try:
        q = _get_queue(request)
        await q.kill(id, reason="Deleted by user")
    except Exception:
        pass
    out_root = _output_root()
    try:
        from dubbing_pipeline.ops.retention import delete_job_artifacts

        deleted, _bytes, paths, unsafe = delete_job_artifacts(job=job, output_root=out_root)
        if unsafe:
            raise HTTPException(
                status_code=400, detail="Refusing to delete outside output dir"
            ) from None
        if not deleted:
            raise HTTPException(status_code=500, detail="Failed to delete job artifacts")
    except HTTPException:
        raise
    except Exception as ex:
        raise HTTPException(status_code=500, detail="Failed to delete job artifacts") from ex
    store.delete_job(id)
    audit_event(
        "job.delete",
        request=request,
        user_id=ident.user.id,
        meta={"job_id": id, "owner_id": str(getattr(job, "owner_id", "") or ""), "paths": paths},
    )
    return {"ok": True}


@router.post("/api/jobs/{id}/visibility")
async def set_job_visibility(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    vis_raw = str(body.get("visibility") or "").strip().lower()
    if vis_raw == "public":
        vis_raw = "shared"
    vis = normalize_visibility(vis_raw).value
    if vis not in {"private", "shared"}:
        raise HTTPException(status_code=400, detail="visibility must be shared|private")
    store.update(str(id), visibility=vis)
    # Best-effort manifest updates (both library and job root).
    with suppress(Exception):
        lib_path = get_library_root_for_job(job) / "manifest.json"
        update_manifest_visibility(lib_path, vis)
    with suppress(Exception):
        out_path = get_job_output_root(job) / "manifest.json"
        update_manifest_visibility(out_path, vis)
    audit_event(
        "job.visibility",
        request=request,
        user_id=ident.user.id,
        meta={"job_id": str(id), "visibility": vis},
    )
    return {"ok": True, "job_id": str(id), "visibility": vis}


@router.post("/api/jobs/{id}/cancel")
async def cancel_job(
    request: Request, id: str, ident: Identity = Depends(require_scope("submit:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    queue = _get_queue(request)
    _ = require_job_access(store=store, ident=ident, job_id=id)
    # Level-2: set cancel flag in Redis so other instances can observe it.
    qb = getattr(request.app.state, "queue_backend", None)
    with suppress(Exception):
        if qb is not None:
            await qb.cancel_job(job_id=str(id), user_id=str(ident.user.id))
    await queue.cancel(id)
    job = require_job_access(store=store, ident=ident, job_id=id)
    return job.to_dict()


@router.post("/api/jobs/{id}/pause")
async def pause_job(
    request: Request, id: str, ident: Identity = Depends(require_scope("submit:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    queue = _get_queue(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    if job.state != JobState.QUEUED:
        raise HTTPException(status_code=409, detail="Can only pause QUEUED jobs")
    j2 = await queue.pause(id)
    if j2 is None:
        raise HTTPException(status_code=404, detail="Not found")
    return j2.to_dict()


@router.post("/api/jobs/{id}/resume")
async def resume_job(
    request: Request, id: str, ident: Identity = Depends(require_scope("submit:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    queue = _get_queue(request)
    scheduler = _get_scheduler(request)
    qb = getattr(request.app.state, "queue_backend", None)
    job = require_job_access(store=store, ident=ident, job_id=id)
    if job.state != JobState.PAUSED:
        raise HTTPException(status_code=409, detail="Can only resume PAUSED jobs")
    j2 = await queue.resume(id)
    if j2 is None:
        raise HTTPException(status_code=404, detail="Not found")
    # Re-submit via canonical queue backend (best-effort).
    with suppress(Exception):
        if qb is not None:
            await qb.submit_job(
                job_id=str(id),
                user_id=str(ident.user.id),
                mode=str(j2.mode),
                device=str(j2.device),
                priority=100,
                meta={"user_role": str(getattr(ident.user.role, "value", "") or "")},
            )
        else:
            scheduler.submit(
                JobRecord(
                    job_id=id,
                    mode=j2.mode,
                    device_pref=j2.device,
                    created_at=time.time(),
                    priority=100,
                )
            )
    return j2.to_dict()
