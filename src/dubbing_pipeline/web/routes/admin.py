from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from dubbing_pipeline.api.access import require_job_access
from dubbing_pipeline.api.deps import Identity, require_role, require_scope
from dubbing_pipeline.api.middleware import audit_event
from dubbing_pipeline.api.models import Role
from dubbing_pipeline.jobs.models import JobState
from dubbing_pipeline.queue.submit_helpers import submit_job_or_503
from dubbing_pipeline.security import quotas
from dubbing_pipeline.web.routes.jobs_common import (
    _get_queue,
    _get_store,
    _new_short_id,
    _now_iso,
)

router = APIRouter()


@router.put("/api/jobs/{id}/tags")
async def set_job_tags(
    request: Request, id: str, ident: Identity = Depends(require_role(Role.operator))
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    body = await request.json()
    tags_in = body.get("tags") if isinstance(body, dict) else None
    if not isinstance(tags_in, list):
        raise HTTPException(status_code=400, detail="tags must be a list")
    tags: list[str] = []
    for t in tags_in[:20]:
        s = str(t).strip()
        if not s:
            continue
        if len(s) > 32:
            s = s[:32]
        tags.append(s)
    rt = dict(job.runtime or {})
    rt["tags"] = tags
    store.update(id, runtime=rt)
    audit_event(
        "job.tags", request=request, user_id=ident.user.id, meta={"job_id": id, "count": len(tags)}
    )
    return {"ok": True, "tags": tags}


@router.post("/api/jobs/{id}/archive")
async def archive_job(
    request: Request, id: str, ident: Identity = Depends(require_role(Role.operator))
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    rt = dict(job.runtime or {})
    rt["archived"] = True
    rt["archived_at"] = _now_iso()
    store.update(id, runtime=rt)
    audit_event("job.archive", request=request, user_id=ident.user.id, meta={"job_id": id})
    return {"ok": True}


@router.post("/api/jobs/{id}/unarchive")
async def unarchive_job(
    request: Request, id: str, ident: Identity = Depends(require_role(Role.operator))
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    rt = dict(job.runtime or {})
    rt["archived"] = False
    rt["archived_at"] = None
    store.update(id, runtime=rt)
    audit_event("job.unarchive", request=request, user_id=ident.user.id, meta={"job_id": id})
    return {"ok": True}


@router.post("/api/jobs/{id}/kill")
async def kill_job_admin(
    request: Request, id: str, ident: Identity = Depends(require_role(Role.admin))
) -> dict[str, Any]:
    """
    Admin-only force-stop.
    This is stronger than "cancel" in that it is allowed even when the operator role is restricted.
    """
    store = _get_store(request)
    queue = _get_queue(request)
    await queue.kill(id, reason="Killed by admin")
    job = require_job_access(store=store, ident=ident, job_id=id)
    audit_event("job.kill", request=request, user_id=ident.user.id, meta={"job_id": id})
    return job.to_dict()


@router.post("/api/jobs/{id}/two_pass/rerun")
async def rerun_two_pass_admin(
    request: Request, id: str, ident: Identity = Depends(require_role(Role.admin))
) -> dict[str, Any]:
    """
    Admin-only: request a pass-2 rerun (TTS + mix only) using extracted/overridden voice refs.
    This does not create a new job; it re-queues the existing job with a runtime marker.
    """
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)

    # Mark runtime so the worker can detect this is a pass-2-only rerun.
    rt = dict(job.runtime or {})
    rt.setdefault("two_pass", {})
    if isinstance(rt.get("two_pass"), dict):
        rt["two_pass"]["request"] = "rerun_pass2"
        rt["two_pass"]["requested_at"] = time.time()
        rt["two_pass"]["requested_by"] = str(ident.user.id)
    else:
        rt["two_pass"] = {
            "request": "rerun_pass2",
            "requested_at": time.time(),
            "requested_by": str(ident.user.id),
        }
    # Ensure enabled for this job.
    rt["voice_clone_two_pass"] = True

    store.update(
        id,
        runtime=rt,
        state=JobState.QUEUED,
        progress=0.0,
        message="Queued (pass 2 rerun)",
    )

    enforcer = quotas.QuotaEnforcer.from_request(request=request, user=ident.user)
    await enforcer.require_concurrent_jobs(action="jobs.rerun_admin")
    await submit_job_or_503(
        request,
        job_id=str(id),
        user_id=str(getattr(job, "owner_id", "") or ""),
        mode=str(job.mode),
        device=str(job.device),
        priority=110,
        meta={
            "user_role": str(getattr(ident.user.role, "value", "") or ""),
            "two_pass": "rerun_pass2",
        },
    )
    audit_event("two_pass.rerun", request=request, user_id=ident.user.id, meta={"job_id": id})
    job2 = require_job_access(store=store, ident=ident, job_id=id)
    return job2.to_dict()


@router.get("/api/presets")
async def list_presets(
    request: Request, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    owner = None if ident.user.role == Role.admin else ident.user.id
    items = store.list_presets(owner_id=owner)
    return {"items": items}


@router.post("/api/presets")
async def create_preset(
    request: Request, ident: Identity = Depends(require_role(Role.admin))
) -> dict[str, Any]:
    store = _get_store(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    name = str(body.get("name") or "").strip() or "Preset"
    owner_id = str(body.get("owner_id") or ident.user.id).strip()
    preset = {
        "id": _new_short_id("preset_"),
        "owner_id": owner_id,
        "name": name,
        "created_at": _now_iso(),
        "mode": str(body.get("mode") or "medium"),
        "device": str(body.get("device") or "auto"),
        "src_lang": str(body.get("src_lang") or "ja"),
        "tgt_lang": str(body.get("tgt_lang") or "en"),
        "tts_lang": str(body.get("tts_lang") or "en"),
        "tts_speaker": str(body.get("tts_speaker") or "default"),
        "tts_speaker_wav": str(body.get("tts_speaker_wav") or ""),
    }
    store.put_preset(preset)
    return preset


@router.delete("/api/presets/{id}")
async def delete_preset(
    request: Request, id: str, ident: Identity = Depends(require_role(Role.admin))
) -> dict[str, Any]:
    store = _get_store(request)
    p = store.get_preset(id)
    if p is None:
        raise HTTPException(status_code=404, detail="Not found")
    store.delete_preset(id)
    return {"ok": True}


@router.get("/api/projects")
async def list_projects(
    request: Request, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    owner = None if ident.user.role == Role.admin else ident.user.id
    items = store.list_projects(owner_id=owner)
    return {"items": items}


@router.post("/api/projects")
async def create_project(
    request: Request, ident: Identity = Depends(require_role(Role.admin))
) -> dict[str, Any]:
    store = _get_store(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    name = str(body.get("name") or "").strip() or "Project"
    default_preset_id = str(body.get("default_preset_id") or "").strip()
    output_subdir = str(body.get("output_subdir") or "").strip()  # under Output/
    owner_id = str(body.get("owner_id") or ident.user.id).strip()
    proj = {
        "id": _new_short_id("proj_"),
        "owner_id": owner_id,
        "name": name,
        "created_at": _now_iso(),
        "default_preset_id": default_preset_id,
        "output_subdir": output_subdir,
    }
    store.put_project(proj)
    return proj


@router.delete("/api/projects/{id}")
async def delete_project(
    request: Request, id: str, ident: Identity = Depends(require_role(Role.admin))
) -> dict[str, Any]:
    store = _get_store(request)
    p = store.get_project(id)
    if p is None:
        raise HTTPException(status_code=404, detail="Not found")
    store.delete_project(id)
    return {"ok": True}
