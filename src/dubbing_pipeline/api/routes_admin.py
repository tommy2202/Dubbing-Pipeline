from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from dubbing_pipeline.api.deps import Identity, require_role
from dubbing_pipeline.api.models import Role
from dubbing_pipeline.ops import audit
from dubbing_pipeline.runtime.scheduler import Scheduler
from dubbing_pipeline.utils.log import logger

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _store(request: Request):
    st = getattr(request.app.state, "job_store", None)
    if st is None:
        raise HTTPException(status_code=500, detail="Job store not initialized")
    return st


def _queue(request: Request):
    q = getattr(request.app.state, "job_queue", None)
    if q is None:
        raise HTTPException(status_code=500, detail="Job queue not initialized")
    return q


def _scheduler(request: Request) -> Scheduler:
    s = getattr(request.app.state, "scheduler", None)
    if s is None:
        s = Scheduler.instance_optional()
    if s is None:
        raise HTTPException(status_code=500, detail="Scheduler not initialized")
    return s


def _queue_backend(request: Request):
    qb = getattr(request.app.state, "queue_backend", None)
    return qb


@router.get("/queue")
async def admin_queue(
    request: Request,
    limit: int = 200,
    ident: Identity = Depends(require_role(Role.admin)),
):
    qb = _queue_backend(request)
    if qb is not None:
        snap = await qb.admin_snapshot(limit=int(limit))
        logger.info(
            "admin_queue_view",
            user_id=str(ident.user.id),
            count=int(len((snap or {}).get("pending") or []) + len((snap or {}).get("running") or [])),
        )
        return {"backend": snap}

    # Fallback to in-proc scheduler view (legacy).
    sched = _scheduler(request)
    store = _store(request)
    items = sched.snapshot_queue(limit=int(limit))
    out = []
    for it in items:
        jid = str(it.get("job_id") or "")
        job = store.get(jid) if jid else None
        out.append(
            {
                **it,
                "owner_user_id": (str(job.owner_id) if job else ""),
                "status": (job.state.value if job else ""),
                "created_at": (str(job.created_at) if job else ""),
                "updated_at": (str(job.updated_at) if job else ""),
            }
        )
    return {"backend": {"mode": "fallback", "pending": out, "running": [], "counts": {"pending": len(out)}}}


@router.post("/jobs/{id}/priority")
async def admin_job_priority(
    request: Request,
    id: str,
    ident: Identity = Depends(require_role(Role.admin)),
) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    try:
        priority = int(body.get("priority"))
    except Exception:
        raise HTTPException(status_code=400, detail="priority must be an int") from None
    priority = max(0, min(1000, int(priority)))
    qb = _queue_backend(request)
    ok = False
    if qb is not None:
        ok = bool(await qb.admin_set_priority(job_id=str(id), priority=int(priority)))
    else:
        sched = _scheduler(request)
        ok = bool(sched.reprioritize(job_id=str(id), priority=int(priority)))
    if not ok:
        raise HTTPException(status_code=404, detail="Job not queued (or not found)")
    audit.emit(
        "admin.job_priority",
        user_id=str(ident.user.id),
        job_id=str(id),
        meta={"priority": int(priority)},
    )
    logger.info("admin_job_priority", user_id=str(ident.user.id), job_id=str(id), priority=int(priority))
    return {"ok": True, "job_id": str(id), "priority": int(priority)}


@router.post("/jobs/{id}/cancel")
async def admin_job_cancel(
    request: Request,
    id: str,
    ident: Identity = Depends(require_role(Role.admin)),
) -> dict:
    qb = _queue_backend(request)
    sched = _scheduler(request)
    q = _queue(request)
    removed = 0
    try:
        removed = int(sched.drop(job_id=str(id)))
    except Exception:
        removed = 0
    # Redis-backed queue cancel flag (cross-instance), best-effort.
    if qb is not None:
        with __import__("contextlib").suppress(Exception):
            await qb.cancel_job(job_id=str(id), user_id=str(ident.user.id))
    try:
        await q.kill(str(id), reason="Canceled by admin")
    except Exception:
        # kill is best-effort; cancellation may still happen via state update
        pass
    audit.emit("admin.job_cancel", user_id=str(ident.user.id), job_id=str(id), meta={"removed": removed})
    logger.info("admin_job_cancel", user_id=str(ident.user.id), job_id=str(id), removed=int(removed))
    return {"ok": True, "job_id": str(id), "removed_from_queue": int(removed)}


@router.post("/users/{id}/quotas")
async def admin_user_quotas(
    request: Request,
    id: str,
    ident: Identity = Depends(require_role(Role.admin)),
) -> dict:
    """
    Set per-user queue quotas (Redis-backed when Redis queue is active).

    Body JSON:
      - max_running: int (optional)
      - max_queued: int (optional)
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    max_running = body.get("max_running")
    max_queued = body.get("max_queued")
    try:
        mr = int(max_running) if max_running is not None else None
    except Exception:
        raise HTTPException(status_code=400, detail="max_running must be int") from None
    try:
        mq = int(max_queued) if max_queued is not None else None
    except Exception:
        raise HTTPException(status_code=400, detail="max_queued must be int") from None

    qb = _queue_backend(request)
    if qb is None:
        raise HTTPException(status_code=400, detail="queue backend not available")
    out = await qb.admin_set_user_quotas(user_id=str(id), max_running=mr, max_queued=mq)
    audit.emit(
        "admin.user_quotas",
        user_id=str(ident.user.id),
        meta={"target_user_id": str(id), **out},
    )
    return {"ok": True, "user_id": str(id), "quotas": out}


@router.post("/jobs/{id}/visibility")
async def admin_job_visibility(
    request: Request,
    id: str,
    ident: Identity = Depends(require_role(Role.admin)),
) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    vis = str(body.get("visibility") or "").strip().lower()
    if vis not in {"public", "private"}:
        raise HTTPException(status_code=400, detail="visibility must be public|private")
    store = _store(request)
    job = store.get(str(id))
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    store.update(str(id), visibility=vis)
    audit.emit(
        "admin.job_visibility",
        user_id=str(ident.user.id),
        job_id=str(id),
        meta={"visibility": vis},
    )
    logger.info("admin_job_visibility", user_id=str(ident.user.id), job_id=str(id), visibility=vis)
    return {"ok": True, "job_id": str(id), "visibility": vis}

