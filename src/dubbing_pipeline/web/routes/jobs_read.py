from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from dubbing_pipeline.api.access import require_job_access
from dubbing_pipeline.api.deps import Identity, require_scope
from dubbing_pipeline.jobs.models import Job
from dubbing_pipeline.security import policy
from dubbing_pipeline.web.routes.jobs_common import _get_store, _player_job_for_path

router = APIRouter(
    dependencies=[
        Depends(policy.require_request_allowed),
        Depends(policy.require_invite_member),
    ]
)


@router.get("/api/jobs")
async def list_jobs(
    request: Request,
    state: str | None = None,
    status: str | None = None,
    q: str | None = None,
    project: str | None = None,
    mode: str | None = None,
    tag: str | None = None,
    include_archived: int = 0,
    limit: int = 25,
    offset: int = 0,
    ident: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    st = status or state
    limit_i = max(1, min(200, int(limit)))
    offset_i = max(0, int(offset))
    jobs_all = store.list(limit=1000, state=st)
    # Default: hide archived unless explicitly included.
    if not bool(int(include_archived or 0)):
        jobs_all = [
            j
            for j in jobs_all
            if not (isinstance(j.runtime, dict) and bool((j.runtime or {}).get("archived")))
        ]

    proj_q = str(project or "").strip().lower()
    mode_q = str(mode or "").strip().lower()
    tag_q = str(tag or "").strip().lower()
    text_q = str(q or "").lower().strip()
    if proj_q or mode_q or tag_q or text_q:
        out2: list[Job] = []
        for j in jobs_all:
            rt = j.runtime if isinstance(j.runtime, dict) else {}
            proj = ""
            if isinstance(rt, dict):
                if isinstance(rt.get("project"), dict):
                    proj = str((rt.get("project") or {}).get("name") or "").strip()
                if not proj:
                    proj = str(rt.get("project_name") or "").strip()
            tags = []
            if isinstance(rt, dict):
                t = rt.get("tags")
                if isinstance(t, list):
                    tags = [str(x).strip().lower() for x in t if str(x).strip()]
            if proj_q and proj_q not in proj.lower():
                continue
            if mode_q and mode_q != str(j.mode or "").strip().lower():
                continue
            if tag_q and tag_q not in set(tags):
                continue
            if text_q:
                hay = " ".join(
                    [
                        str(j.id or ""),
                        str(j.video_path or ""),
                        proj,
                        " ".join(tags),
                    ]
                ).lower()
                if text_q not in hay:
                    continue
            out2.append(j)
        jobs_all = out2
    visible: list[Job] = []
    for j in jobs_all:
        try:
            require_job_access(store=store, ident=ident, job=j)
        except HTTPException as ex:
            if ex.status_code == 403:
                continue
            raise
        visible.append(j)
    jobs_all = visible
    total = len(jobs_all)
    jobs = jobs_all[offset_i : offset_i + limit_i]
    out = []
    for j in jobs:
        rt = j.runtime if isinstance(j.runtime, dict) else {}
        tags = []
        if isinstance(rt, dict) and isinstance(rt.get("tags"), list):
            tags = [str(x).strip() for x in (rt.get("tags") or []) if str(x).strip()]
        out.append(
            {
                "id": j.id,
                "state": j.state,
                "progress": j.progress,
                "message": j.message,
                "video_path": j.video_path,
                "created_at": j.created_at,
                "updated_at": j.updated_at,
                "output_mkv": j.output_mkv,
                "mode": j.mode,
                "src_lang": j.src_lang,
                "tgt_lang": j.tgt_lang,
                "device": j.device,
                "runtime": j.runtime,
                "tags": tags,
            }
        )
    next_offset = offset_i + limit_i
    return {
        "items": out,
        "limit": limit_i,
        "offset": offset_i,
        "total": total,
        "next_offset": (next_offset if next_offset < total else None),
    }


@router.get("/api/project-profiles")
async def list_project_profiles(_: Identity = Depends(require_scope("read:job"))) -> dict[str, Any]:
    """
    Filesystem-backed project profiles under <APP_ROOT>/projects/<name>/profile.yaml.
    Returned items are safe to expose (no secrets).
    """
    try:
        from dubbing_pipeline.projects.loader import list_project_profiles as _list
        from dubbing_pipeline.projects.loader import load_project_profile

        items: list[dict[str, Any]] = []
        for name in _list():
            try:
                prof = load_project_profile(name)
                if prof is None:
                    continue
                items.append({"name": prof.name, "profile_hash": prof.profile_hash})
            except Exception:
                continue
        return {"items": items}
    except Exception:
        return {"items": []}


@router.get("/api/jobs/{id}")
async def get_job(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    d = job.to_dict()
    # Attach checkpoint (best-effort) for stage breakdown.
    with suppress(Exception):
        from dubbing_pipeline.jobs.checkpoint import read_ckpt

        base_dir = Path(job.work_dir) if job.work_dir else None
        if base_dir:
            ckpt_path = (base_dir / ".checkpoint.json").resolve()
            ck = read_ckpt(id, ckpt_path=ckpt_path)
            if ck:
                d["checkpoint"] = ck
    # Provide player id for existing output files (if under OUTPUT_ROOT).
    with suppress(Exception):
        omkv = Path(str(job.output_mkv)) if job.output_mkv else None
        if omkv and omkv.exists():
            pj = _player_job_for_path(omkv)
            if pj:
                d["player_job"] = pj
    return d
