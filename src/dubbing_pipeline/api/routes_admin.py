from __future__ import annotations

import secrets
import time

from fastapi import Depends, HTTPException, Request

from dubbing_pipeline.api.deps import Identity, get_limiter
from dubbing_pipeline.api.invites import invite_token_hash
from dubbing_pipeline.api.models import Role
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.security import policy
from dubbing_pipeline.security.policy_deps import secure_router
from dubbing_pipeline.ops import audit
from dubbing_pipeline.ops.metrics import job_errors
from dubbing_pipeline.jobs.models import JobState
from dubbing_pipeline.runtime.scheduler import Scheduler
from dubbing_pipeline.utils.log import logger
from dubbing_pipeline.utils.net import get_client_ip

router = secure_router(prefix="/api/admin", tags=["admin"])

_INVITE_TTL_DEFAULT_HOURS = 24
_INVITE_TTL_MAX_HOURS = 168


def _store(request: Request):
    st = getattr(request.app.state, "job_store", None)
    if st is None:
        raise HTTPException(status_code=500, detail="Job store not initialized")
    return st


def _auth_store(request: Request):
    st = getattr(request.app.state, "auth_store", None)
    if st is None:
        raise HTTPException(status_code=500, detail="Auth store not initialized")
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


def _job_queue(request: Request):
    q = getattr(request.app.state, "job_queue", None)
    return q


def _collect_job_error_counts() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for metric in job_errors.collect():
            for sample in metric.samples:
                if not sample.name.endswith("_total"):
                    continue
                stage = sample.labels.get("stage") if isinstance(sample.labels, dict) else None
                if not stage:
                    continue
                out[str(stage)] = int(sample.value)
    except Exception:
        return {}
    return out


def _infer_failure_stage(log_tail: str, err: str | None) -> str:
    text = "\n".join([str(err or ""), log_tail]).lower()
    stages = [
        "audio",
        "audio_extractor",
        "diarize",
        "transcribe",
        "translate",
        "post_translate",
        "tts",
        "mix",
        "mux",
        "lipsync",
        "qa",
        "drift_report",
        "retention",
        "voice_refs",
        "separation",
    ]
    for st in stages:
        if f"{st} failed" in text or f"stage={st}" in text or f"{st} watchdog" in text:
            return st
    if "timeout" in text:
        if "tts" in text:
            return "tts"
        if "whisper" in text or "transcribe" in text:
            return "transcribe"
        if "audio" in text:
            return "audio"
    return "unknown"


@router.get("/queue")
async def admin_queue(
    request: Request,
    limit: int = 200,
    ident: Identity = Depends(policy.require_admin),
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
    ident: Identity = Depends(policy.require_admin),
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
    ident: Identity = Depends(policy.require_admin),
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
    ident: Identity = Depends(policy.require_admin),
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


@router.get("/reports")
async def admin_list_reports(
    request: Request,
    status: str | None = "open",
    limit: int = 200,
    offset: int = 0,
    ident: Identity = Depends(policy.require_admin),
) -> dict:
    store = _store(request)
    items = store.list_library_reports(limit=int(limit), offset=int(offset), status=status)
    return {"ok": True, "items": items}


@router.get("/reports/summary")
async def admin_reports_summary(
    request: Request,
    ident: Identity = Depends(policy.require_admin),
) -> dict:
    store = _store(request)
    s = get_settings()
    count_open = store.count_library_reports(status="open")
    admin_topic = str(getattr(s, "ntfy_admin_topic", "") or "").strip()
    ntfy_configured = bool(getattr(s, "ntfy_enabled", False)) and bool(admin_topic)
    return {"ok": True, "open_reports": int(count_open), "ntfy_admin_configured": ntfy_configured}


@router.post("/reports/{id}/resolve")
async def admin_resolve_report(
    request: Request,
    id: str,
    ident: Identity = Depends(policy.require_admin),
) -> dict:
    store = _store(request)
    store.update_report_status(str(id), status="resolved")
    audit.emit(
        "admin.report_resolved",
        user_id=str(ident.user.id),
        meta={"report_id": str(id)},
    )
    return {"ok": True, "report_id": str(id)}


@router.get("/quotas/{user_id}")
async def admin_get_user_quota(
    request: Request,
    user_id: str,
    ident: Identity = Depends(policy.require_admin),
) -> dict:
    store = _store(request)
    quotas = store.get_user_quota(str(user_id))
    used = int(store.get_user_storage_bytes(str(user_id)) or 0)
    return {"ok": True, "user_id": str(user_id), "quotas": quotas, "storage_bytes": used}


@router.post("/quotas/{user_id}")
async def admin_set_user_quota(
    request: Request,
    user_id: str,
    ident: Identity = Depends(policy.require_admin),
) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    clear = bool(body.get("clear") or False)
    store = _store(request)
    existing = store.get_user_quota(str(user_id))
    missing = object()

    def _parse_int(val: object, *, field: str) -> int | None:
        if val is missing:
            return existing.get(field) if not clear else None
        if val is None:
            return None
        try:
            return max(0, int(val))  # type: ignore[arg-type]
        except Exception:
            raise HTTPException(status_code=400, detail=f"{field} must be int") from None

    max_upload_bytes = _parse_int(body.get("max_upload_bytes", missing), field="max_upload_bytes")
    jobs_per_day = _parse_int(body.get("jobs_per_day", missing), field="jobs_per_day")
    max_concurrent_jobs = _parse_int(
        body.get("max_concurrent_jobs", missing), field="max_concurrent_jobs"
    )
    max_storage_bytes = _parse_int(
        body.get("max_storage_bytes", missing), field="max_storage_bytes"
    )

    quotas = store.upsert_user_quota(
        str(user_id),
        max_upload_bytes=max_upload_bytes,
        jobs_per_day=jobs_per_day,
        max_concurrent_jobs=max_concurrent_jobs,
        max_storage_bytes=max_storage_bytes,
        updated_by=str(ident.user.id),
    )
    # Optional: also sync max_concurrent into queue backend (max_running).
    qb = _queue_backend(request)
    if qb is not None and max_concurrent_jobs is not None:
        with __import__("contextlib").suppress(Exception):
            await qb.admin_set_user_quotas(
                user_id=str(user_id),
                max_running=int(max_concurrent_jobs),
                max_queued=None,
            )
    audit.emit(
        "admin.user_quota_overrides",
        user_id=str(ident.user.id),
        meta={"target_user_id": str(user_id), **{k: v for k, v in quotas.items() if v is not None}},
    )
    used = int(store.get_user_storage_bytes(str(user_id)) or 0)
    return {"ok": True, "user_id": str(user_id), "quotas": quotas, "storage_bytes": used}


@router.post("/jobs/{id}/visibility")
async def admin_job_visibility(
    request: Request,
    id: str,
    ident: Identity = Depends(policy.require_admin),
) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    vis = str(body.get("visibility") or "").strip().lower()
    if vis == "public":
        vis = "shared"
    if vis not in {"shared", "private"}:
        raise HTTPException(status_code=400, detail="visibility must be shared|private")
    policy.require_share_allowed(user=ident.user, visibility_value=vis)
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


@router.get("/glossaries")
async def admin_list_glossaries(
    request: Request,
    language_pair: str | None = None,
    series_slug: str | None = None,
    ident: Identity = Depends(policy.require_admin),
) -> dict:
    store = _store(request)
    items = store.list_glossaries(
        language_pair=str(language_pair or "").strip().lower() or None,
        series_slug=str(series_slug or "").strip() or None,
        enabled_only=False,
    )
    return {"ok": True, "items": items}


@router.post("/glossaries")
async def admin_create_glossary(
    request: Request,
    ident: Identity = Depends(policy.require_admin),
) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    gid = str(body.get("id") or "").strip() or f"gloss_{secrets.token_hex(8)}"
    name = str(body.get("name") or "").strip()
    language_pair = str(body.get("language_pair") or "").strip().lower()
    if not name or not language_pair or "->" not in language_pair:
        raise HTTPException(status_code=400, detail="name and language_pair are required")
    rules = body.get("rules_json") if "rules_json" in body else body.get("rules")
    if rules is None:
        raise HTTPException(status_code=400, detail="rules_json is required")
    series_slug = str(body.get("series_slug") or "").strip() or None
    priority = int(body.get("priority") or 0)
    enabled = bool(body.get("enabled", True))
    store = _store(request)
    rec = store.upsert_glossary(
        glossary_id=gid,
        name=name,
        language_pair=language_pair,
        series_slug=series_slug,
        priority=priority,
        enabled=enabled,
        rules_json=rules,
    )
    audit.emit(
        "admin.glossary.create",
        user_id=str(ident.user.id),
        meta={"glossary_id": gid, "language_pair": language_pair, "series_slug": series_slug},
    )
    return {"ok": True, "item": rec}


@router.put("/glossaries/{id}")
async def admin_update_glossary(
    request: Request,
    id: str,
    ident: Identity = Depends(policy.require_admin),
) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    name = str(body.get("name") or "").strip()
    language_pair = str(body.get("language_pair") or "").strip().lower()
    if not name or not language_pair or "->" not in language_pair:
        raise HTTPException(status_code=400, detail="name and language_pair are required")
    rules = body.get("rules_json") if "rules_json" in body else body.get("rules")
    if rules is None:
        raise HTTPException(status_code=400, detail="rules_json is required")
    series_slug = str(body.get("series_slug") or "").strip() or None
    priority = int(body.get("priority") or 0)
    enabled = bool(body.get("enabled", True))
    store = _store(request)
    rec = store.upsert_glossary(
        glossary_id=str(id),
        name=name,
        language_pair=language_pair,
        series_slug=series_slug,
        priority=priority,
        enabled=enabled,
        rules_json=rules,
    )
    audit.emit(
        "admin.glossary.update",
        user_id=str(ident.user.id),
        meta={"glossary_id": str(id), "language_pair": language_pair, "series_slug": series_slug},
    )
    return {"ok": True, "item": rec}


@router.delete("/glossaries/{id}")
async def admin_delete_glossary(
    request: Request,
    id: str,
    ident: Identity = Depends(policy.require_admin),
) -> dict:
    store = _store(request)
    ok = bool(store.delete_glossary(str(id)))
    audit.emit(
        "admin.glossary.delete",
        user_id=str(ident.user.id),
        meta={"glossary_id": str(id)},
    )
    return {"ok": ok}


@router.get("/pronunciation")
async def admin_list_pronunciation(
    request: Request,
    lang: str | None = None,
    ident: Identity = Depends(policy.require_admin),
) -> dict:
    store = _store(request)
    items = store.list_pronunciations(lang=str(lang or "").strip().lower() or None)
    return {"ok": True, "items": items}


@router.post("/pronunciation")
async def admin_create_pronunciation(
    request: Request,
    ident: Identity = Depends(policy.require_admin),
) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    entry_id = str(body.get("id") or "").strip() or f"pron_{secrets.token_hex(8)}"
    lang = str(body.get("lang") or "").strip().lower()
    term = str(body.get("term") or "").strip()
    ipa = body.get("ipa_or_phoneme")
    if ipa is None:
        ipa = {"format": str(body.get("format") or "ipa"), "value": str(body.get("value") or "")}
    if not lang or not term:
        raise HTTPException(status_code=400, detail="lang and term are required")
    store = _store(request)
    rec = store.upsert_pronunciation(
        entry_id=entry_id,
        lang=lang,
        term=term,
        ipa_or_phoneme=ipa,
        example=str(body.get("example") or ""),
        created_by=str(ident.user.id),
    )
    audit.emit(
        "admin.pronunciation.create",
        user_id=str(ident.user.id),
        meta={"entry_id": entry_id, "lang": lang},
    )
    return {"ok": True, "item": rec}


@router.put("/pronunciation/{id}")
async def admin_update_pronunciation(
    request: Request,
    id: str,
    ident: Identity = Depends(policy.require_admin),
) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    lang = str(body.get("lang") or "").strip().lower()
    term = str(body.get("term") or "").strip()
    ipa = body.get("ipa_or_phoneme")
    if ipa is None:
        ipa = {"format": str(body.get("format") or "ipa"), "value": str(body.get("value") or "")}
    if not lang or not term:
        raise HTTPException(status_code=400, detail="lang and term are required")
    store = _store(request)
    rec = store.upsert_pronunciation(
        entry_id=str(id),
        lang=lang,
        term=term,
        ipa_or_phoneme=ipa,
        example=str(body.get("example") or ""),
        created_by=str(ident.user.id),
    )
    audit.emit(
        "admin.pronunciation.update",
        user_id=str(ident.user.id),
        meta={"entry_id": str(id), "lang": lang},
    )
    return {"ok": True, "item": rec}


@router.delete("/pronunciation/{id}")
async def admin_delete_pronunciation(
    request: Request,
    id: str,
    ident: Identity = Depends(policy.require_admin),
) -> dict:
    store = _store(request)
    ok = bool(store.delete_pronunciation(str(id)))
    audit.emit(
        "admin.pronunciation.delete",
        user_id=str(ident.user.id),
        meta={"entry_id": str(id)},
    )
    return {"ok": ok}


@router.get("/metrics")
async def admin_metrics(
    request: Request, ident: Identity = Depends(policy.require_admin)
) -> dict[str, Any]:
    store = _store(request)
    qb = _queue_backend(request)
    q = _job_queue(request)
    snapshot: dict[str, Any] = {}
    if qb is not None:
        try:
            snapshot = await qb.admin_snapshot(limit=200)
        except Exception:
            snapshot = {}

    # Queue depth + active jobs (fallback to JobStore).
    queue_counts = {"queued": 0, "running": 0}
    if isinstance(snapshot, dict):
        if "counts" in snapshot and isinstance(snapshot.get("counts"), dict):
            counts = snapshot.get("counts") or {}
            queue_counts["queued"] = int(counts.get("pending") or 0)
            queue_counts["running"] = int(counts.get("running") or 0)
        elif "items" in snapshot and isinstance(snapshot.get("items"), list):
            running = 0
            queued = 0
            for it in snapshot.get("items") or []:
                st = str(it.get("state") or "").upper()
                if st == "RUNNING":
                    running += 1
                if st == "QUEUED":
                    queued += 1
            queue_counts["queued"] = queued
            queue_counts["running"] = running

    active_jobs: list[dict[str, Any]] = []
    try:
        for j in store.list(limit=500):
            if j.state not in {JobState.RUNNING, JobState.QUEUED}:
                continue
            active_jobs.append(
                {
                    "job_id": str(j.id),
                    "user_id": str(j.owner_id or ""),
                    "state": str(j.state.value),
                    "progress": float(j.progress or 0.0),
                    "message": str(j.message or ""),
                    "series_slug": str(getattr(j, "series_slug", "") or ""),
                    "season_number": int(getattr(j, "season_number", 0) or 0),
                    "episode_number": int(getattr(j, "episode_number", 0) or 0),
                }
            )
    except Exception:
        active_jobs = []

    q_status = None
    if qb is not None:
        try:
            q_status = qb.status()
        except Exception:
            q_status = None
    worker_health = {
        "queue_mode": str(getattr(q_status, "mode", "unknown") or "unknown"),
        "backend_ok": bool(getattr(q_status, "redis_ok", False)),
        "workers_total": len(getattr(q, "_tasks", []) or []) if q is not None else 0,
        "workers_alive": len([t for t in (getattr(q, "_tasks", []) or []) if not t.done()])
        if q is not None
        else 0,
        "timestamp": float(time.time()),
    }

    storage_usage = []
    try:
        storage_usage = store.list_user_storage(limit=200)
    except Exception:
        storage_usage = []

    failures_by_stage = _collect_job_error_counts()

    return {
        "ok": True,
        "queue": {
            "depth": int(queue_counts.get("queued", 0) + queue_counts.get("running", 0)),
            "queued": int(queue_counts.get("queued", 0)),
            "running": int(queue_counts.get("running", 0)),
            "snapshot_mode": str(snapshot.get("mode") or ""),
        },
        "active_jobs": active_jobs,
        "failures_by_stage": failures_by_stage,
        "worker_health": worker_health,
        "storage_usage": storage_usage,
    }


@router.get("/jobs/failures")
async def admin_job_failures(
    request: Request,
    limit: int = 200,
    ident: Identity = Depends(policy.require_admin),
) -> dict[str, Any]:
    store = _store(request)
    lim = max(1, min(1000, int(limit)))
    items: list[dict[str, Any]] = []
    try:
        for j in store.list(limit=lim * 2):
            if j.state != JobState.FAILED:
                continue
            tail = store.tail_log(str(j.id), n=120)
            stage = _infer_failure_stage(tail, j.error)
            items.append(
                {
                    "job_id": str(j.id),
                    "user_id": str(j.owner_id or ""),
                    "stage": str(stage),
                    "error": str(j.error or ""),
                    "message": str(j.message or ""),
                    "series_slug": str(getattr(j, "series_slug", "") or ""),
                    "season_number": int(getattr(j, "season_number", 0) or 0),
                    "episode_number": int(getattr(j, "episode_number", 0) or 0),
                    "log_tail": tail,
                }
            )
            if len(items) >= lim:
                break
    except Exception:
        items = []
    return {"ok": True, "items": items, "limit": lim}


@router.get("/voices/suggestions")
async def admin_list_voice_profile_suggestions(
    request: Request,
    status: str | None = "pending",
    limit: int = 200,
    offset: int = 0,
    ident: Identity = Depends(policy.require_admin),
) -> dict[str, Any]:
    store = _store(request)
    status_eff = None if not status or status == "all" else str(status)
    items = store.list_voice_profile_suggestions_all(
        status=status_eff, limit=int(limit), offset=int(offset)
    )
    out: list[dict[str, Any]] = []
    for rec in items:
        pid = str(rec.get("voice_profile_id") or "")
        sid = str(rec.get("suggested_profile_id") or "")
        prof = store.get_voice_profile(pid) if pid else None
        sugg = store.get_voice_profile(sid) if sid else None
        out.append(
            {
                "id": str(rec.get("id") or ""),
                "profile_id": pid,
                "suggested_profile_id": sid,
                "similarity": float(rec.get("similarity") or 0.0),
                "status": str(rec.get("status") or ""),
                "created_at": rec.get("created_at"),
                "updated_at": rec.get("updated_at"),
                "profile_display_name": (
                    str(prof.get("display_name") or "") if isinstance(prof, dict) else ""
                ),
                "profile_series_lock": (
                    str(prof.get("series_lock") or "") if isinstance(prof, dict) else ""
                ),
                "suggested_display_name": (
                    str(sugg.get("display_name") or "") if isinstance(sugg, dict) else ""
                ),
                "suggested_series_lock": (
                    str(sugg.get("series_lock") or "") if isinstance(sugg, dict) else ""
                ),
                "suggested_scope": (
                    str(sugg.get("scope") or "") if isinstance(sugg, dict) else ""
                ),
            }
        )
    return {"ok": True, "items": out, "limit": int(limit), "offset": int(offset)}


@router.post("/voices/approve_merge")
async def admin_approve_voice_profile_merge(
    request: Request,
    ident: Identity = Depends(policy.require_admin),
) -> dict[str, Any]:
    store = _store(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    suggestion_id = str(body.get("suggestion_id") or "").strip()
    if not suggestion_id:
        raise HTTPException(status_code=422, detail="suggestion_id required")
    sugg = store.get_voice_profile_suggestion(suggestion_id)
    if sugg is None:
        raise HTTPException(status_code=404, detail="suggestion not found")
    if str(sugg.get("status") or "") != "accepted":
        raise HTTPException(status_code=409, detail="Suggestion not accepted by owner")
    vp = str(sugg.get("voice_profile_id") or "")
    ap = str(sugg.get("suggested_profile_id") or "")
    alias = None
    if not store.has_voice_profile_alias(vp, ap):
        alias = store.upsert_voice_profile_alias(
            voice_profile_id=vp,
            alias_of_voice_profile_id=ap,
            confidence=float(sugg.get("similarity") or 0.0),
            approved_by_admin=True,
            approved_at=time.time(),
        )
    else:
        alias = store.approve_voice_profile_alias(vp, ap)
    store.set_voice_profile_suggestion_status(suggestion_id, status="approved")
    audit.emit(
        "admin.voice_profile.approve_merge",
        user_id=str(ident.user.id),
        meta={"suggestion_id": suggestion_id, "voice_profile_id": vp, "alias_of": ap},
    )
    return {"ok": True, "alias": alias, "suggestion_id": suggestion_id}


@router.get("/invites")
async def admin_list_invites(
    request: Request,
    limit: int = 200,
    offset: int = 0,
    ident: Identity = Depends(policy.require_admin),
) -> dict[str, object]:
    store = _auth_store(request)
    items = store.list_invites(limit=int(limit), offset=int(offset))
    now = int(time.time())
    out: list[dict[str, object]] = []
    for it in items:
        token_hash = str(it.get("token_hash") or "")
        created_at = int(it.get("created_at") or 0)
        expires_at = int(it.get("expires_at") or 0)
        used_at = int(it.get("used_at") or 0) if it.get("used_at") else None
        status = "active"
        if used_at:
            status = "used"
        elif expires_at and expires_at < now:
            status = "expired"
        out.append(
            {
                "token_hash_prefix": token_hash[:8] if token_hash else "",
                "created_by": str(it.get("created_by") or ""),
                "created_at": created_at,
                "expires_at": expires_at,
                "used_at": used_at,
                "used_by": str(it.get("used_by") or ""),
                "status": status,
            }
        )
    return {"items": out, "limit": int(limit), "offset": int(offset)}


@router.post("/invites")
async def admin_create_invite(
    request: Request, ident: Identity = Depends(policy.require_admin)
) -> dict[str, object]:
    body = await request.json()
    if not isinstance(body, dict):
        body = {}
    ttl_in = body.get("expires_in_hours")
    try:
        ttl_hours = int(ttl_in) if ttl_in is not None else _INVITE_TTL_DEFAULT_HOURS
    except Exception:
        raise HTTPException(status_code=400, detail="expires_in_hours must be int") from None
    ttl_hours = max(1, min(int(ttl_hours), int(_INVITE_TTL_MAX_HOURS)))

    rl = get_limiter(request)
    ip = get_client_ip(request)
    if not rl.allow(f"invites:create:admin:{ident.user.id}", limit=20, per_seconds=60):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    if not rl.allow(f"invites:create:ip:{ip}", limit=60, per_seconds=60):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    token = secrets.token_urlsafe(32)
    token_hash = invite_token_hash(token)
    created_at = int(time.time())
    expires_at = int(created_at + ttl_hours * 3600)
    store = _auth_store(request)
    store.create_invite(
        token_hash=token_hash,
        created_by=str(ident.user.id),
        created_at=int(created_at),
        expires_at=int(expires_at),
    )

    s = get_settings()
    base = str(getattr(s, "public_base_url", "") or "").strip().rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    invite_url = f"{base}/invite/{token}"

    audit.emit(
        "invite.create",
        user_id=str(ident.user.id),
        meta={
            "expires_at": int(expires_at),
            "ttl_hours": int(ttl_hours),
            "token_hash_prefix": token_hash[:8],
        },
    )
    return {
        "ok": True,
        "invite_url": invite_url,
        "created_at": int(created_at),
        "expires_at": int(expires_at),
        "token_hash_prefix": token_hash[:8],
    }

