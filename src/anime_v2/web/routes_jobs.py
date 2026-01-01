from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import PlainTextResponse
from sse_starlette.sse import EventSourceResponse  # type: ignore

from anime_v2.jobs.models import Job, JobState, new_id, now_utc
from anime_v2.api.deps import Identity, require_scope
from anime_v2.api.models import AuthStore
from anime_v2.api.security import decode_token
from anime_v2.config import get_settings
from anime_v2.jobs.limits import concurrent_jobs_for_user, get_limits, used_minutes_today
from anime_v2.utils.ffmpeg_safe import FFmpegError, ffprobe_duration_seconds
from anime_v2.ops.metrics import jobs_queued
from anime_v2.ops.storage import ensure_free_space
from anime_v2.utils.log import request_id_var
from anime_v2.utils.crypto import verify_secret
from anime_v2.utils.ratelimit import RateLimiter
from anime_v2.runtime.scheduler import JobRecord, Scheduler
from anime_v2.runtime import lifecycle


router = APIRouter()

_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")
_ALLOWED_UPLOAD_MIME = {
    "video/mp4",
    "video/quicktime",
    "video/x-matroska",
    "video/webm",
    "application/octet-stream",  # some browsers
}


def _app_root() -> Path:
    env = os.environ.get("APP_ROOT")
    if env:
        return Path(env).resolve()
    if Path("/app").exists():
        return Path("/app").resolve()
    return Path.cwd().resolve()


def _sanitize_video_path(p: str) -> Path:
    if not p or not _SAFE_PATH_RE.match(p):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid video_path")

    root = _app_root()
    raw = Path(p)
    if raw.is_absolute():
        resolved = raw.resolve()
        try:
            resolved.relative_to(root)
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="video_path must be under APP_ROOT")
        return resolved

    resolved = (root / raw).resolve()
    try:
        resolved.relative_to(root)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="video_path must be under APP_ROOT")
    return resolved


def _get_store(request: Request):
    store = getattr(request.app.state, "job_store", None)
    if store is None:
        raise HTTPException(status_code=500, detail="Job store not initialized")
    return store


def _get_queue(request: Request):
    q = getattr(request.app.state, "job_queue", None)
    if q is None:
        raise HTTPException(status_code=500, detail="Job queue not initialized")
    return q


def _get_scheduler(request: Request):
    s = getattr(request.app.state, "scheduler", None)
    if s is None:
        # scheduler should be installed in server lifespan; fall back to singleton
        s = Scheduler.instance_optional()
    if s is None:
        raise HTTPException(status_code=500, detail="Scheduler not initialized")
    return s


@router.post("/api/jobs")
async def create_job(request: Request, ident: Identity = Depends(require_scope("submit:job"))) -> dict[str, str]:
    # Idempotency-Key: return existing job when present and not expired.
    idem_key = (request.headers.get("idempotency-key") or "").strip()
    store = _get_store(request)
    if idem_key:
        # basic bounds to avoid abuse
        if len(idem_key) > 200:
            raise HTTPException(status_code=400, detail="Idempotency-Key too long")
        ttl = int(os.environ.get("IDEMPOTENCY_TTL_SEC", "86400"))
        hit = store.get_idempotency(idem_key)
        if hit:
            jid, ts = hit
            if (time.time() - ts) <= ttl:
                if store.get(jid) is not None:
                    return {"id": jid}

    # Draining: do not accept new jobs (but idempotency hits above still return).
    if lifecycle.is_draining():
        ra = str(lifecycle.retry_after_seconds(60))
        raise HTTPException(status_code=503, detail="Server is draining; try again later", headers={"Retry-After": ra})

    # Rate limit: 10/min per identity (fallback to IP)
    rl: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if rl is None:
        rl = RateLimiter()
        request.app.state.rate_limiter = rl
    who = ident.user.id if ident.kind == "user" else (ident.api_key_prefix or "unknown")
    if not rl.allow(f"jobs:submit:{who}", limit=10, per_seconds=60):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    # Disk guard: refuse new jobs when storage is low.
    s = get_settings()
    out_root = Path(str(getattr(store, "db_path", Path(os.environ.get("ANIME_V2_OUTPUT_DIR", "Output"))))).resolve().parent
    out_root.mkdir(parents=True, exist_ok=True)
    ensure_free_space(min_gb=int(s.min_free_gb), path=out_root)

    scheduler = _get_scheduler(request)
    limits = get_limits()

    ctype = (request.headers.get("content-type") or "").lower()
    mode = "medium"
    device = "auto"
    src_lang = "auto"
    tgt_lang = "en"
    video_path: Path | None = None
    duration_s = 0.0

    if "application/json" in ctype:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON")
        mode = str(body.get("mode") or mode)
        device = str(body.get("device") or device)
        src_lang = str(body.get("src_lang") or src_lang)
        tgt_lang = str(body.get("tgt_lang") or tgt_lang)
        vp = body.get("video_path")
        if not isinstance(vp, str):
            raise HTTPException(status_code=400, detail="Missing video_path")
        video_path = _sanitize_video_path(vp)
        if not video_path.exists():
            raise HTTPException(status_code=400, detail="video_path does not exist")
        if not video_path.is_file():
            raise HTTPException(status_code=400, detail="video_path must be a file")
    else:
        # multipart/form-data
        form = await request.form()
        mode = str(form.get("mode") or mode)
        device = str(form.get("device") or device)
        src_lang = str(form.get("src_lang") or src_lang)
        tgt_lang = str(form.get("tgt_lang") or tgt_lang)

        file = form.get("file")
        vp = form.get("video_path")
        if file is None and vp is None:
            raise HTTPException(status_code=400, detail="Provide file or video_path")

        if vp is not None:
            video_path = _sanitize_video_path(str(vp))
            if not video_path.exists():
                raise HTTPException(status_code=400, detail="video_path does not exist")
            if not video_path.is_file():
                raise HTTPException(status_code=400, detail="video_path must be a file")
        else:
            # Save upload to Input/uploads/<uuid>.mp4 under APP_ROOT
            upload = file  # starlette.datastructures.UploadFile
            ctype_u = (getattr(upload, "content_type", None) or "").lower().strip()
            if ctype_u and ctype_u not in _ALLOWED_UPLOAD_MIME:
                raise HTTPException(status_code=400, detail=f"Unsupported upload content-type: {ctype_u}")
            root = _app_root()
            up_dir = (root / "Input" / "uploads").resolve()
            up_dir.mkdir(parents=True, exist_ok=True)
            jid = new_id()
            ext = ".mp4"
            name = getattr(upload, "filename", "") or ""
            if "." in name:
                ext = "." + name.rsplit(".", 1)[-1][:8]
            dest = up_dir / f"{jid}{ext}"
            max_bytes = int(limits.max_upload_mb) * 1024 * 1024
            written = 0
            try:
                with dest.open("wb") as f:
                    while True:
                        chunk = await upload.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            raise HTTPException(status_code=400, detail=f"Upload too large (>{limits.max_upload_mb}MB)")
                        f.write(chunk)
            except HTTPException:
                try:
                    dest.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
            video_path = dest

    assert video_path is not None
    # Validate duration using ffprobe (no user-controlled args).
    try:
        duration_s = float(ffprobe_duration_seconds(video_path))
    except FFmpegError as ex:
        raise HTTPException(status_code=400, detail=f"Invalid media file (ffprobe failed): {ex}")
    if duration_s <= 0.5:
        raise HTTPException(status_code=400, detail="Video duration is too short or unreadable")
    if duration_s > float(limits.max_video_min) * 60.0:
        raise HTTPException(status_code=400, detail=f"Video too long (> {limits.max_video_min} minutes)")

    jid = new_id()
    created = now_utc()

    # Per-user quotas (concurrency + daily processing minutes)
    all_jobs = store.list(limit=1000)
    conc = concurrent_jobs_for_user(all_jobs, user_id=ident.user.id)
    if conc >= limits.max_concurrent_per_user:
        raise HTTPException(status_code=429, detail=f"Too many concurrent jobs (limit={limits.max_concurrent_per_user})")
    used_min = used_minutes_today(all_jobs, user_id=ident.user.id, now_iso=created)
    req_min = duration_s / 60.0
    if (used_min + req_min) > float(limits.daily_processing_minutes):
        raise HTTPException(
            status_code=429,
            detail=f"Daily quota exceeded (limit={limits.daily_processing_minutes} min, used={used_min:.1f} min, requested={req_min:.1f} min)",
        )

    job = Job(
        id=jid,
        owner_id=ident.user.id,
        video_path=str(video_path),
        duration_s=float(duration_s),
        request_id=(request_id_var.get() or ""),
        mode=mode,
        device=device,
        src_lang=src_lang,
        tgt_lang=tgt_lang,
        created_at=created,
        updated_at=created,
        state=JobState.QUEUED,
        progress=0.0,
        message="Queued",
        output_mkv="",
        output_srt="",
        work_dir="",
        log_path="",
        error=None,
    )
    store.put(job)
    if idem_key:
        store.put_idempotency(idem_key, jid)
    try:
        scheduler.submit(
            JobRecord(
                job_id=jid,
                mode=mode,
                device_pref=device,
                created_at=time.time(),
                priority=100,
            )
        )
    except RuntimeError as ex:
        if "draining" in str(ex).lower():
            ra = str(lifecycle.retry_after_seconds(60))
            raise HTTPException(status_code=503, detail="Server is draining; try again later", headers={"Retry-After": ra})
        raise
    jobs_queued.inc()
    return {"id": jid}


@router.get("/api/jobs")
async def list_jobs(request: Request, state: str | None = None, _: Identity = Depends(require_scope("read:job"))) -> list[dict[str, Any]]:
    store = _get_store(request)
    jobs = store.list(limit=100, state=state)
    out = []
    for j in jobs:
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
            }
        )
    return out


@router.get("/api/jobs/{id}")
async def get_job(request: Request, id: str, _: Identity = Depends(require_scope("read:job"))) -> dict[str, Any]:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    return job.to_dict()


@router.post("/api/jobs/{id}/cancel")
async def cancel_job(request: Request, id: str, _: Identity = Depends(require_scope("submit:job"))) -> dict[str, Any]:
    store = _get_store(request)
    queue = _get_queue(request)
    await queue.cancel(id)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    return job.to_dict()


@router.get("/api/jobs/{id}/logs/tail")
async def tail_logs(request: Request, id: str, n: int = 200, _: Identity = Depends(require_scope("read:job"))) -> PlainTextResponse:
    store = _get_store(request)
    return PlainTextResponse(store.tail_log(id, n=n))


@router.websocket("/ws/jobs/{id}")
async def ws_job(websocket: WebSocket, id: str):
    await websocket.accept()
    token = websocket.query_params.get("token") or ""
    if not token:
        await websocket.close(code=1008)
        return

    # Authenticate: JWT access token OR dp_ API key in token param.
    auth_store: AuthStore | None = getattr(websocket.app.state, "auth_store", None)
    if auth_store is None:
        await websocket.close(code=1011)
        return
    ok = False
    try:
        if token.startswith("dp_"):
            parts = token.split("_", 2)
            if len(parts) == 3:
                _, prefix, _ = parts
                for k in auth_store.find_api_keys_by_prefix(prefix):
                    if verify_secret(k.key_hash, token):
                        # require read scope
                        scopes = set(k.scopes or [])
                        if "admin:*" in scopes or "read:job" in scopes:
                            ok = True
                        break
        else:
            data = decode_token(token, expected_typ="access")
            scopes = data.get("scopes") if isinstance(data.get("scopes"), list) else []
            scopes = {str(s) for s in scopes}
            if "admin:*" in scopes or "read:job" in scopes:
                ok = True
    except Exception:
        ok = False

    if not ok:
        await websocket.close(code=1008)
        return

    store = getattr(websocket.app.state, "job_store", None)
    if store is None:
        await websocket.close(code=1011)
        return

    last_updated = None
    try:
        while True:
            job = store.get(id)
            if job is None:
                await websocket.send_json({"error": "not_found"})
                await websocket.close()
                return

            if job.updated_at != last_updated:
                last_updated = job.updated_at
                await websocket.send_json(
                    {
                        "id": job.id,
                        "state": job.state,
                        "progress": job.progress,
                        "message": job.message,
                        "updated_at": job.updated_at,
                    }
                )

            if job.state in {JobState.DONE, JobState.FAILED, JobState.CANCELED}:
                await asyncio.sleep(0.2)
                return

            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        return


@router.get("/events/jobs/{id}")
async def sse_job(request: Request, id: str, _: Identity = Depends(require_scope("read:job"))):
    store = _get_store(request)

    async def gen():
        last_updated = None
        while True:
            if await request.is_disconnected():
                return
            job = store.get(id)
            if job is None:
                yield {"event": "message", "data": '{"error":"not_found"}'}
                return
            if job.updated_at != last_updated:
                last_updated = job.updated_at
                data = {
                    "id": job.id,
                    "state": job.state,
                    "progress": job.progress,
                    "message": job.message,
                    "updated_at": job.updated_at,
                }
                yield {"event": "message", "data": json.dumps(data)}
            if job.state in {JobState.DONE, JobState.FAILED, JobState.CANCELED}:
                return
            await asyncio.sleep(0.5)

    import json

    return EventSourceResponse(gen())

