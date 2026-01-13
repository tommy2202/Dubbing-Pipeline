from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from anime_v2.api.deps import Identity, require_role
from anime_v2.api.models import Role
from anime_v2.ops import audit
from anime_v2.runtime.scheduler import Scheduler
from anime_v2.utils.log import logger

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


@router.get("/queue")
async def admin_queue(
    request: Request,
    limit: int = 200,
    ident: Identity = Depends(require_role(Role.admin)),
):
    sched = _scheduler(request)
    store = _store(request)
    items = sched.snapshot_queue(limit=int(limit))
    # Enrich with best-effort job metadata for admin visibility.
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
    logger.info("admin_queue_view", user_id=str(ident.user.id), count=len(out))
    return {"state": sched.state(), "items": out}


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
    sched = _scheduler(request)
    ok = sched.reprioritize(job_id=str(id), priority=int(priority))
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
    sched = _scheduler(request)
    q = _queue(request)
    removed = 0
    try:
        removed = int(sched.drop(job_id=str(id)))
    except Exception:
        removed = 0
    try:
        await q.kill(str(id), reason="Canceled by admin")
    except Exception:
        # kill is best-effort; cancellation may still happen via state update
        pass
    audit.emit("admin.job_cancel", user_id=str(ident.user.id), job_id=str(id), meta={"removed": removed})
    logger.info("admin_job_cancel", user_id=str(ident.user.id), job_id=str(id), removed=int(removed))
    return {"ok": True, "job_id": str(id), "removed_from_queue": int(removed)}


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

