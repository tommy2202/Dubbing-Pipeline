from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from dubbing_pipeline.api.access import require_job_access
from dubbing_pipeline.api.deps import Identity
from dubbing_pipeline.api.middleware import audit_event
from dubbing_pipeline.jobs.models import JobState, now_utc
from dubbing_pipeline.queue.submit_helpers import submit_job_or_503
from dubbing_pipeline.security import policy
from dubbing_pipeline.utils.log import logger
from dubbing_pipeline.web.routes.jobs_common import (
    _enforce_rate_limit,
    _get_store,
    _job_base_dir,
    _load_transcript_store,
)


async def post_job_segments_rerun(
    request: Request, id: str, ident: Identity
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw = body.get("segment_ids") if "segment_ids" in body else body.get("segments")
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="segment_ids must be a list")
    segment_ids: list[int] = []
    for v in raw:
        try:
            sid = int(v)
        except Exception:
            continue
        if sid > 0:
            segment_ids.append(int(sid))
    segment_ids = sorted({int(x) for x in segment_ids})
    if not segment_ids:
        raise HTTPException(status_code=400, detail="segment_ids must be non-empty")

    transcript_store = _load_transcript_store(base_dir)
    resynth = {
        "type": "segments",
        "segment_ids": segment_ids,
        "requested_at": now_utc(),
        "transcript_version": int(transcript_store.get("version") or 0),
    }
    warning = None
    try:
        from dubbing_pipeline.qa.scoring import find_latest_tts_manifest_path

        if find_latest_tts_manifest_path(base_dir) is None:
            warning = "Segment-only rerun not available; rerunning full job."
            resynth["fallback"] = "full"
    except Exception:
        warning = "Segment-only rerun not available; rerunning full job."
        resynth["fallback"] = "full"

    rt = dict(job.runtime or {})
    rt["resynth"] = resynth
    job2 = store.update(
        id,
        state=JobState.QUEUED,
        progress=0.0,
        message="Segment resynth requested",
        runtime=rt,
    )
    await policy.require_concurrent_jobs(
        request=request, user=ident.user, action="jobs.rerun"
    )
    await submit_job_or_503(
        request,
        job_id=str(id),
        user_id=str(ident.user.id),
        mode=str((job2.mode if job2 else job.mode)),
        device=str((job2.device if job2 else job.device)),
        priority=50,
        meta={"user_role": str(getattr(ident.user.role, "value", "") or "")},
    )
    logger.info(
        "qa_segments_rerun_request",
        job_id=str(id),
        segment_count=len(segment_ids),
        warning=warning,
    )
    audit_event(
        "qa.segments.rerun",
        request=request,
        user_id=ident.user.id,
        meta={"job_id": id, "segment_count": len(segment_ids), "warning": warning},
    )
    return {"ok": True, "segment_ids": segment_ids, "warning": warning}


async def synthesize_from_approved(
    request: Request, id: str, ident: Identity
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    st = _load_transcript_store(base_dir)
    # Mark job to re-synthesize only approved segments.
    rt = dict(job.runtime or {})
    rt["resynth"] = {
        "type": "approved",
        "requested_at": now_utc(),
        "transcript_version": int(st.get("version") or 0),
    }
    job2 = store.update(
        id,
        state=JobState.QUEUED,
        progress=0.0,
        message="Resynth requested (approved only)",
        runtime=rt,
    )
    await policy.require_concurrent_jobs(
        request=request, user=ident.user, action="jobs.resynth"
    )
    await submit_job_or_503(
        request,
        job_id=str(id),
        user_id=str(ident.user.id),
        mode=str((job2.mode if job2 else job.mode)),
        device=str((job2.device if job2 else job.device)),
        priority=50,
        meta={"user_role": str(getattr(ident.user.role, "value", "") or "")},
    )
    return {"ok": True}


async def post_job_review_regen(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity,
) -> dict[str, Any]:
    store = _get_store(request)
    _enforce_rate_limit(
        request,
        key=f"review:regen:user:{ident.user.id}",
        limit=60,
        per_seconds=60,
    )
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    from dubbing_pipeline.review.ops import regen_segment

    try:
        p = regen_segment(base_dir, int(segment_id))
        audit_event(
            "review.regen",
            request=request,
            user_id=ident.user.id,
            meta={"job_id": id, "segment_id": int(segment_id)},
        )
        return {"ok": True, "audio_path": str(p)}
    except Exception as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex
