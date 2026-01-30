from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sse_starlette.sse import EventSourceResponse  # type: ignore

from dubbing_pipeline.api.access import require_job_access
from dubbing_pipeline.api.deps import Identity, require_scope
from dubbing_pipeline.jobs.models import JobState
from dubbing_pipeline.security.policy_deps import secure_router
from dubbing_pipeline.runtime import lifecycle
from dubbing_pipeline.web.routes.jobs_common import _get_store, _job_base_dir, _parse_iso_ts

router = secure_router()


@router.get("/api/jobs/{id}/timeline")
async def get_job_timeline(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    ckpt_path = (base_dir / ".checkpoint.json").resolve()
    ckpt = None
    with suppress(Exception):
        from dubbing_pipeline.jobs.checkpoint import read_ckpt

        if ckpt_path.exists():
            ckpt = read_ckpt(id, ckpt_path=ckpt_path)
    stages = ckpt.get("stages") if isinstance(ckpt, dict) else {}
    if not isinstance(stages, dict):
        stages = {}

    # Runtime skipped stages (best-effort)
    skip_map: dict[str, str] = {}
    rt = dict(job.runtime or {})
    skips = rt.get("skipped_stages")
    if isinstance(skips, list):
        for it in skips:
            if not isinstance(it, dict):
                continue
            k = str(it.get("stage") or "").strip()
            v = str(it.get("reason") or "").strip()
            if k and v and k not in skip_map:
                skip_map[k] = v

    stage_defs = [
        {"key": "queued", "label": "Queued", "ckpt": []},
        {"key": "extract", "label": "Extract", "ckpt": ["audio"]},
        {"key": "asr", "label": "ASR", "ckpt": ["transcribe"]},
        {"key": "translate", "label": "Translate", "ckpt": ["translate", "post_translate"]},
        {"key": "tts", "label": "TTS", "ckpt": ["tts"]},
        {"key": "mix", "label": "Mix", "ckpt": ["mix"]},
        {"key": "export", "label": "Export", "ckpt": ["mux", "export"]},
    ]

    def _entry_for(defn: dict[str, Any]) -> dict[str, Any]:
        entry = None
        for cand in defn.get("ckpt", []):
            it = stages.get(str(cand))
            if isinstance(it, dict):
                entry = dict(it)
                break
        status = None
        started_at = None
        ended_at = None
        reason = None
        if entry:
            status = str(entry.get("status") or "").lower() or None
            started_at = entry.get("started_at")
            ended_at = entry.get("done_at") or entry.get("skipped_at")
            reason = str(entry.get("skip_reason") or "").strip() or None
            if not status:
                status = "done" if entry.get("done") else None
        if not reason:
            reason = skip_map.get(defn["key"])
        if reason:
            status = "skipped"
        if status == "started":
            status = "running"
        if not status:
            status = "pending"
        duration_s = None
        try:
            if started_at and ended_at and float(ended_at) >= float(started_at):
                duration_s = float(ended_at) - float(started_at)
        except Exception:
            duration_s = None
        return {
            "key": defn["key"],
            "label": defn["label"],
            "status": status,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_s": duration_s,
            "reason": reason,
        }

    timeline = [_entry_for(d) for d in stage_defs]

    # Fill queued timestamps from job metadata (ISO -> epoch seconds)
    queued = timeline[0]
    created_ts = _parse_iso_ts(job.created_at) or _parse_iso_ts(job.updated_at)
    queued["started_at"] = queued.get("started_at") or created_ts
    first_stage_ts = None
    for st in timeline[1:]:
        ts = st.get("started_at") or st.get("ended_at")
        if ts:
            first_stage_ts = ts if first_stage_ts is None else min(float(first_stage_ts), float(ts))
    if first_stage_ts and queued.get("ended_at") is None and job.state != JobState.QUEUED:
        queued["ended_at"] = first_stage_ts
    if queued.get("started_at") and queued.get("ended_at"):
        try:
            queued["duration_s"] = float(queued["ended_at"]) - float(queued["started_at"])
        except Exception:
            queued["duration_s"] = None

    # Backwards compat: if export has no entry but mix is done, mark export skipped.
    export_entry = timeline[-1]
    mix_entry = timeline[-2]
    if export_entry.get("status") == "pending" and mix_entry.get("status") == "done":
        export_entry["status"] = "skipped"
        export_entry["reason"] = export_entry.get("reason") or "combined_with_mix"

    # Determine current stage
    current_idx = None
    for i, st in enumerate(timeline):
        if st.get("status") == "running":
            current_idx = i
            break
    if current_idx is None and job.state == JobState.QUEUED:
        current_idx = 0
        timeline[0]["status"] = "running"
    if current_idx is None and job.state == JobState.RUNNING:
        for i, st in enumerate(timeline):
            if st.get("status") == "pending":
                current_idx = i
                st["status"] = "running"
                break
    if current_idx is not None:
        timeline[current_idx]["current"] = True

    # Last log line (best-effort)
    last_log = ""
    with suppress(Exception):
        tail = store.tail_log(id, n=1).strip()
        last_log = tail[:300] + ("…" if len(tail) > 300 else "")

    def _status_label(s: str) -> str:
        s = str(s or "").lower()
        if s == "running":
            return "In progress"
        if s == "done":
            return "Done"
        if s == "skipped":
            return "Skipped"
        return "Pending"

    for st in timeline:
        st["status_label"] = _status_label(st.get("status"))

    return {
        "job_id": str(job.id),
        "state": str(job.state.value),
        "stages": timeline,
        "last_log_line": last_log,
        "log_tail_url": f"/api/jobs/{id}/logs/tail?n=200",
        "log_stream_url": f"/api/jobs/{id}/logs/stream",
    }


@router.get("/api/jobs/{id}/logs/tail")
async def tail_logs(
    request: Request, id: str, n: int = 200, ident: Identity = Depends(require_scope("read:job"))
) -> PlainTextResponse:
    store = _get_store(request)
    require_job_access(store=store, ident=ident, job_id=id)
    return PlainTextResponse(store.tail_log(id, n=n))


@router.get("/api/jobs/{id}/logs")
async def logs_alias(
    request: Request, id: str, n: int = 200, ident: Identity = Depends(require_scope("read:job"))
) -> PlainTextResponse:
    # Alias for mobile clients: tail-only.
    return await tail_logs(request, id, n=n, ident=ident)


@router.get("/api/jobs/{id}/logs/stream")
async def stream_logs(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
):
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    log_path = Path(job.log_path) if job.log_path else None
    if log_path is None:
        raise HTTPException(status_code=404, detail="No logs for job")

    once = (request.query_params.get("once") or "").strip() == "1"

    async def gen():
        pos = 0
        # initial tail
        with suppress(Exception):
            txt = store.tail_log(id, n=200)
            for ln in txt.splitlines():
                yield {"event": "message", "data": f"<div>{ln}</div>"}
        if once:
            return
        try:
            while True:
                if lifecycle.is_draining():
                    return
                if await request.is_disconnected():
                    return
                with suppress(Exception):
                    if log_path.exists() and log_path.is_file():
                        with log_path.open("r", encoding="utf-8", errors="replace") as f:
                            f.seek(pos)
                            chunk = f.read()
                            pos = f.tell()
                        if chunk:
                            for ln in chunk.splitlines():
                                yield {"event": "message", "data": f"<div>{ln}</div>"}
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return

    return EventSourceResponse(gen())
