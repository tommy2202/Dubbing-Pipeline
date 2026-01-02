from __future__ import annotations

import asyncio
import hashlib
import io
import re
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import PlainTextResponse, Response
from sse_starlette.sse import EventSourceResponse  # type: ignore

from anime_v2.api.deps import Identity, require_role, require_scope
from anime_v2.api.models import AuthStore, Role
from anime_v2.api.security import decode_token
from anime_v2.config import get_settings
from anime_v2.jobs.limits import concurrent_jobs_for_user, get_limits, used_minutes_today
from anime_v2.jobs.models import Job, JobState, new_id, now_utc
from anime_v2.ops.metrics import jobs_queued, pipeline_job_total
from anime_v2.ops.storage import ensure_free_space
from anime_v2.runtime import lifecycle
from anime_v2.runtime.scheduler import JobRecord, Scheduler
from anime_v2.utils.crypto import verify_secret
from anime_v2.utils.ffmpeg_safe import FFmpegError, ffprobe_duration_seconds
from anime_v2.utils.log import request_id_var
from anime_v2.utils.ratelimit import RateLimiter

router = APIRouter()

_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")
_ALLOWED_UPLOAD_MIME = {
    "video/mp4",
    "video/quicktime",
    "video/x-matroska",
    "video/webm",
    "application/octet-stream",  # some browsers
}


def _now_iso() -> str:
    return now_utc()


def _new_short_id(prefix: str = "p_") -> str:
    import secrets

    return prefix + secrets.token_hex(8)


def _app_root() -> Path:
    return Path(get_settings().app_root).resolve()


def _input_dir() -> Path:
    """
    Base directory for user-provided inputs under APP_ROOT.
    """
    s = get_settings()
    root = _app_root()
    if getattr(s, "input_dir", None):
        try:
            return Path(str(s.input_dir)).resolve()
        except Exception:
            pass
    return (root / "Input").resolve()


def _input_uploads_dir() -> Path:
    """
    Directory where the web UI/API stores uploads.
    """
    s = get_settings()
    if getattr(s, "input_uploads_dir", None):
        try:
            return Path(str(s.input_uploads_dir)).resolve()
        except Exception:
            pass
    return (_input_dir() / "uploads").resolve()


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
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="video_path must be under APP_ROOT"
            ) from None
        return resolved

    resolved = (root / raw).resolve()
    try:
        resolved.relative_to(root)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="video_path must be under APP_ROOT"
        ) from None
    return resolved


def _sanitize_output_subdir(s: str) -> str:
    s = (s or "").strip().strip("/")
    if not s:
        return ""
    # allow spaces for friendly folder names
    if not re.fullmatch(r"[A-Za-z0-9._/\- ]+", s):
        raise HTTPException(status_code=400, detail="Invalid output_subdir")
    # prevent traversal
    if ".." in s.split("/"):
        raise HTTPException(status_code=400, detail="Invalid output_subdir")
    return s


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


def _output_root() -> Path:
    return Path(get_settings().output_dir).resolve()


def _player_job_for_path(p: Path) -> str | None:
    out_root = _output_root()
    try:
        rp = p.resolve()
        rel = str(rp.relative_to(out_root)).replace("\\", "/")
    except Exception:
        return None
    return hashlib.sha256(rel.encode("utf-8")).hexdigest()[:32]


def _parse_srt(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    blocks = [b for b in text.split("\n\n") if b.strip()]

    def parse_ts(ts: str) -> float:
        hh, mm, rest = ts.split(":")
        ss, ms = rest.split(",")
        return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0

    out: list[dict[str, Any]] = []
    for b in blocks:
        lines = [ln.rstrip("\n") for ln in b.splitlines() if ln.strip()]
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        try:
            start_s, end_s = (p.strip() for p in lines[1].split("-->", 1))
            start = float(parse_ts(start_s))
            end = float(parse_ts(end_s))
            txt = "\n".join(lines[2:]).strip() if len(lines) > 2 else ""
            out.append({"start": start, "end": end, "text": txt})
        except Exception:
            continue
    return out


def _review_state_path(base_dir: Path) -> Path:
    return (base_dir / "review" / "state.json").resolve()


def _review_audio_path(base_dir: Path, segment_id: int) -> Path | None:
    try:
        from anime_v2.review.state import load_state

        st = load_state(base_dir)
        segs = st.get("segments", [])
        if not isinstance(segs, list):
            return None
        for s in segs:
            if isinstance(s, dict) and int(s.get("segment_id") or 0) == int(segment_id):
                p = Path(str(s.get("audio_path_current") or ""))
                return p if p.exists() else None
    except Exception:
        return None
    return None


def _file_range_response(request: Request, path: Path, *, media_type: str) -> Response:
    """
    Minimal HTTP Range support for audio preview.
    """
    data = path.read_bytes()
    size = len(data)
    rng = request.headers.get("range")
    if not rng:
        return Response(content=data, media_type=media_type)
    m = re.match(r"bytes=(\d+)-(\d+)?", rng)
    if not m:
        return Response(content=data, media_type=media_type)
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    start = max(0, min(start, size))
    end = max(start, min(end, size - 1))
    chunk = data[start : end + 1]
    headers = {
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Accept-Ranges": "bytes",
    }
    return Response(content=chunk, status_code=206, headers=headers, media_type=media_type)


def _stream_manifest_path(base_dir: Path) -> Path:
    return (base_dir / "stream" / "manifest.json").resolve()


def _stream_chunk_mp4_path(base_dir: Path, idx: int) -> Path | None:
    """
    idx is 1-based chunk index.
    """
    p = (base_dir / "stream" / f"chunk_{int(idx):03d}.mp4").resolve()
    return p if p.exists() else None


def _fmt_ts_srt(seconds: float) -> str:
    s = max(0.0, float(seconds))
    hh = int(s // 3600)
    mm = int((s % 3600) // 60)
    ss = int(s % 60)
    ms = int(round((s - int(s)) * 1000.0))
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


def _write_srt_segments(path: Path, segments: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for i, s in enumerate(segments, 1):
            f.write(
                f"{i}\n{_fmt_ts_srt(float(s['start']))} --> {_fmt_ts_srt(float(s['end']))}\n{str(s.get('text') or '').strip()}\n\n"
            )


def _job_base_dir(job: Job) -> Path:
    # Prefer parent of output_mkv (stable Output/<stem>/), else use Output/<video_stem>.
    if job.output_mkv:
        with suppress(Exception):
            p = Path(str(job.output_mkv))
            if p.parent.exists():
                return p.parent.resolve()
    try:
        stem = Path(str(job.video_path)).stem
    except Exception:
        stem = job.id
    return (_output_root() / stem).resolve()


def _transcript_store_paths(base_dir: Path) -> tuple[Path, Path]:
    return base_dir / "transcript_store.json", base_dir / "transcript_versions.jsonl"


def _load_transcript_store(base_dir: Path) -> dict[str, Any]:
    store_path, _ = _transcript_store_paths(base_dir)
    if not store_path.exists():
        return {"version": 0, "segments": {}}
    with suppress(Exception):
        import json as _json

        data = _json.loads(store_path.read_text(encoding="utf-8", errors="replace"))
        if isinstance(data, dict):
            data.setdefault("version", 0)
            data.setdefault("segments", {})
            if not isinstance(data["segments"], dict):
                data["segments"] = {}
            return data
    return {"version": 0, "segments": {}}


def _save_transcript_store(base_dir: Path, data: dict[str, Any]) -> None:
    store_path, _ = _transcript_store_paths(base_dir)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    import json as _json

    tmp = store_path.with_suffix(".tmp")
    tmp.write_text(_json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(store_path)


def _append_transcript_version(base_dir: Path, entry: dict[str, Any]) -> None:
    _, vpath = _transcript_store_paths(base_dir)
    vpath.parent.mkdir(parents=True, exist_ok=True)
    import json as _json

    with vpath.open("a", encoding="utf-8") as f:
        f.write(_json.dumps(entry, sort_keys=True) + "\n")


@router.post("/api/jobs")
async def create_job(
    request: Request, ident: Identity = Depends(require_scope("submit:job"))
) -> dict[str, str]:
    # Idempotency-Key: return existing job when present and not expired.
    # Supports header or multipart form field `idempotency_key` (for HTML forms).
    idem_key = (request.headers.get("idempotency-key") or "").strip()
    parsed_form = None
    ctype0 = (request.headers.get("content-type") or "").lower()
    if (
        not idem_key
        and "application/json" not in ctype0
        and ("multipart/form-data" in ctype0 or "application/x-www-form-urlencoded" in ctype0)
    ):
        try:
            parsed_form = await request.form()
            idem_key = str(parsed_form.get("idempotency_key") or "").strip()
        except Exception:
            parsed_form = None
    store = _get_store(request)
    if idem_key:
        # basic bounds to avoid abuse
        if len(idem_key) > 200:
            raise HTTPException(status_code=400, detail="Idempotency-Key too long")
        ttl = int(get_settings().idempotency_ttl_sec)
        hit = store.get_idempotency(idem_key)
        if hit:
            jid, ts = hit
            if (time.time() - ts) <= ttl and store.get(jid) is not None:
                return {"id": jid}

    # Draining: do not accept new jobs (but idempotency hits above still return).
    if lifecycle.is_draining():
        ra = str(lifecycle.retry_after_seconds(60))
        raise HTTPException(
            status_code=503,
            detail="Server is draining; try again later",
            headers={"Retry-After": ra},
        )

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
    out_root = Path(str(getattr(store, "db_path", Path(s.output_dir)))).resolve().parent
    out_root.mkdir(parents=True, exist_ok=True)
    ensure_free_space(min_gb=int(s.min_free_gb), path=out_root)

    scheduler = _get_scheduler(request)
    limits = get_limits()

    ctype = ctype0
    mode = "medium"
    device = "auto"
    src_lang = "auto"
    tgt_lang = "en"
    pg = "off"
    pg_policy_path = ""
    qa = False
    project_name = ""
    style_guide_path = ""
    speaker_smoothing = False
    scene_detect = "audio"
    director = False
    director_strength = 0.5
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
        pg = str(body.get("pg") or pg)
        pg_policy_path = str(body.get("pg_policy_path") or pg_policy_path)
        qa = bool(body.get("qa") or False)
        project_name = str(body.get("project") or body.get("project_name") or "")
        style_guide_path = str(body.get("style_guide_path") or "")
        speaker_smoothing = bool(body.get("speaker_smoothing") or False)
        scene_detect = str(body.get("scene_detect") or scene_detect)
        director = bool(body.get("director") or False)
        director_strength = float(body.get("director_strength") or director_strength)
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
        form = parsed_form or await request.form()
        mode = str(form.get("mode") or mode)
        device = str(form.get("device") or device)
        src_lang = str(form.get("src_lang") or src_lang)
        tgt_lang = str(form.get("tgt_lang") or tgt_lang)
        pg = str(form.get("pg") or pg)
        pg_policy_path = str(form.get("pg_policy_path") or pg_policy_path)
        qa = str(form.get("qa") or "").strip() not in {"", "0", "false", "off"}
        project_name = str(form.get("project") or form.get("project_name") or "")
        style_guide_path = str(form.get("style_guide_path") or "")
        speaker_smoothing = str(form.get("speaker_smoothing") or "").strip() not in {"", "0", "false", "off"}
        scene_detect = str(form.get("scene_detect") or scene_detect)
        director = str(form.get("director") or "").strip() not in {"", "0", "false", "off"}
        director_strength = float(form.get("director_strength") or director_strength)

        file = form.get("file")
        vp = form.get("video_path")
        if file is None and vp is None:
            raise HTTPException(status_code=400, detail="Provide file or video_path")

        if vp is not None:
            # Allow both:
            # - explicit relative paths under APP_ROOT (e.g. "Input/Test.mp4")
            # - bare filenames relative to INPUT_DIR (e.g. "Test.mp4")
            vp_s = str(vp)
            if vp_s and not Path(vp_s).is_absolute() and not vp_s.startswith("Input/"):
                root = _app_root()
                try:
                    rel_input = _input_dir().resolve().relative_to(root)
                    vp_s = str(Path(rel_input) / vp_s)
                except Exception:
                    vp_s = str(Path("Input") / vp_s)
            video_path = _sanitize_video_path(vp_s)
            if not video_path.exists():
                raise HTTPException(status_code=400, detail="video_path does not exist")
            if not video_path.is_file():
                raise HTTPException(status_code=400, detail="video_path must be a file")
        else:
            # Save upload to INPUT_UPLOADS_DIR/<uuid>.mp4 under APP_ROOT
            upload = file  # starlette.datastructures.UploadFile
            ctype_u = (getattr(upload, "content_type", None) or "").lower().strip()
            if ctype_u and ctype_u not in _ALLOWED_UPLOAD_MIME:
                raise HTTPException(
                    status_code=400, detail=f"Unsupported upload content-type: {ctype_u}"
                )
            up_dir = _input_uploads_dir()
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
                            raise HTTPException(
                                status_code=400,
                                detail=f"Upload too large (>{limits.max_upload_mb}MB)",
                            )
                        f.write(chunk)
            except HTTPException:
                with suppress(Exception):
                    dest.unlink(missing_ok=True)
                raise
            video_path = dest

    assert video_path is not None
    # Validate duration using ffprobe (no user-controlled args).
    try:
        duration_s = float(ffprobe_duration_seconds(video_path))
    except FFmpegError as ex:
        raise HTTPException(
            status_code=400, detail=f"Invalid media file (ffprobe failed): {ex}"
        ) from ex
    if duration_s <= 0.5:
        raise HTTPException(status_code=400, detail="Video duration is too short or unreadable")
    if duration_s > float(limits.max_video_min) * 60.0:
        raise HTTPException(
            status_code=400, detail=f"Video too long (> {limits.max_video_min} minutes)"
        )

    jid = new_id()
    created = now_utc()

    # Per-user quotas (concurrency + daily processing minutes)
    all_jobs = store.list(limit=1000)
    conc = concurrent_jobs_for_user(all_jobs, user_id=ident.user.id)
    if conc >= limits.max_concurrent_per_user:
        raise HTTPException(
            status_code=429,
            detail=f"Too many concurrent jobs (limit={limits.max_concurrent_per_user})",
        )
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
    # Per-job (session) flags; NOT persisted as global defaults.
    rt = dict(job.runtime or {})
    pg_norm = str(pg or "off").strip().lower()
    if pg_norm in {"pg13", "pg"}:
        rt["pg"] = pg_norm
        if pg_policy_path.strip():
            rt["pg_policy_path"] = pg_policy_path.strip()
    if bool(qa):
        rt["qa"] = True
    if project_name.strip():
        rt["project_name"] = project_name.strip()
    if style_guide_path.strip():
        rt["style_guide_path"] = style_guide_path.strip()
    if bool(speaker_smoothing):
        rt["speaker_smoothing"] = True
        rt["scene_detect"] = str(scene_detect or "audio").strip().lower()
    if bool(director):
        rt["director"] = True
        rt["director_strength"] = float(director_strength)

    # Store requested + effective settings summary (best-effort; no secrets).
    try:
        from anime_v2.modes import resolve_effective_settings
        from anime_v2.config import get_settings as _gs

        s = _gs()
        base = {
            "diarizer": str(getattr(s, "diarizer", "auto")),
            "speaker_smoothing": bool(getattr(s, "speaker_smoothing", False)),
            "voice_memory": bool(getattr(s, "voice_memory", False)),
            "voice_mode": str(getattr(s, "voice_mode", "clone")),
            "music_detect": bool(getattr(s, "music_detect", False)),
            "separation": str(getattr(s, "separation", "off")),
            "mix_mode": str(getattr(s, "mix_mode", "legacy")),
            "timing_fit": bool(getattr(s, "timing_fit", False)),
            "pacing": bool(getattr(s, "pacing", False)),
            "qa": False,
            "director": bool(getattr(s, "director", False)),
            "multitrack": bool(getattr(s, "multitrack", False)),
            "voice_mode": str(getattr(s, "voice_mode", "clone")),
        }
        overrides: dict[str, Any] = {}
        if bool(qa):
            overrides["qa"] = True
        if bool(speaker_smoothing):
            overrides["speaker_smoothing"] = True
        if bool(director):
            overrides["director"] = True
        eff = resolve_effective_settings(mode=str(mode), base=base, overrides=overrides)
        rt["requested_mode"] = str(mode)
        rt["effective_settings"] = eff.to_dict()
    except Exception:
        pass
    job.runtime = rt
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
            raise HTTPException(
                status_code=503,
                detail="Server is draining; try again later",
                headers={"Retry-After": ra},
            ) from ex
        raise
    jobs_queued.inc()
    pipeline_job_total.inc()
    return {"id": jid}


@router.post("/api/jobs/batch")
async def create_jobs_batch(
    request: Request, ident: Identity = Depends(require_scope("submit:job"))
) -> dict[str, Any]:
    """
    Batch submit jobs.

    Supports:
      - multipart/form-data with:
          - files: multiple UploadFile (field name 'files')
          - preset_id (optional), project_id (optional)
      - application/json with:
          - items: [{video_path|filename, preset_id?, project_id?}, ...]
    """
    store = _get_store(request)
    scheduler = _get_scheduler(request)
    limits = get_limits()

    # Disk guard once per batch
    s = get_settings()
    out_root = Path(str(getattr(store, "db_path", Path(s.output_dir)))).resolve().parent
    out_root.mkdir(parents=True, exist_ok=True)
    ensure_free_space(min_gb=int(s.min_free_gb), path=out_root)

    created_ids: list[str] = []

    async def _submit_one(
        *,
        video_path: Path,
        mode: str,
        device: str,
        src_lang: str,
        tgt_lang: str,
        preset: dict[str, Any] | None,
        project: dict[str, Any] | None,
        pg: str = "off",
        pg_policy_path: str = "",
        qa: bool = False,
        project_name: str = "",
        style_guide_path: str = "",
    ) -> str:
        # duration validation
        try:
            duration_s = float(ffprobe_duration_seconds(video_path))
        except FFmpegError as ex:
            raise HTTPException(
                status_code=400, detail=f"Invalid media file (ffprobe failed): {ex}"
            ) from ex
        if duration_s <= 0.5:
            raise HTTPException(status_code=400, detail="Video duration is too short or unreadable")
        if duration_s > float(limits.max_video_min) * 60.0:
            raise HTTPException(
                status_code=400, detail=f"Video too long (> {limits.max_video_min} minutes)"
            )

        jid = new_id()
        created = now_utc()
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
        rt = dict(job.runtime or {})
        if preset:
            rt["preset_id"] = str(preset.get("id") or "")
            rt["preset"] = {
                "tts_lang": str(preset.get("tts_lang") or ""),
                "tts_speaker": str(preset.get("tts_speaker") or ""),
                "tts_speaker_wav": str(preset.get("tts_speaker_wav") or ""),
            }
        if project:
            rt["project_id"] = str(project.get("id") or "")
            rt["project"] = {
                "name": str(project.get("name") or ""),
                "output_subdir": str(project.get("output_subdir") or ""),
            }
        pg_norm = str(pg or "off").strip().lower()
        if pg_norm in {"pg13", "pg"}:
            rt["pg"] = pg_norm
            if str(pg_policy_path or "").strip():
                rt["pg_policy_path"] = str(pg_policy_path).strip()
        if bool(qa):
            rt["qa"] = True
        if str(project_name or "").strip():
            rt["project_name"] = str(project_name).strip()
        if str(style_guide_path or "").strip():
            rt["style_guide_path"] = str(style_guide_path).strip()
        job.runtime = rt
        store.put(job)
        try:
            scheduler.submit(
                JobRecord(
                    job_id=jid, mode=mode, device_pref=device, created_at=time.time(), priority=100
                )
            )
        except RuntimeError as ex:
            if "draining" in str(ex).lower():
                raise HTTPException(
                    status_code=503, detail="Server is draining; try again later"
                ) from ex
            raise
        jobs_queued.inc()
        pipeline_job_total.inc()
        return jid

    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        for it in body["items"]:
            if not isinstance(it, dict):
                continue
            preset_id = str(it.get("preset_id") or "").strip()
            project_id = str(it.get("project_id") or "").strip()
            preset = store.get_preset(preset_id) if preset_id else None
            project = store.get_project(project_id) if project_id else None
            # Allow `filename` as relative under APP_ROOT/Input/...
            vp = it.get("video_path") or it.get("filename")
            if not isinstance(vp, str):
                raise HTTPException(status_code=400, detail="Missing video_path/filename")
            if vp.startswith("/"):
                video_path = _sanitize_video_path(vp)
            else:
                # Treat filenames as relative to INPUT_DIR for safety/consistency.
                root = _app_root()
                try:
                    rel_input = _input_dir().resolve().relative_to(root)
                    video_path = _sanitize_video_path(str(Path(rel_input) / vp))
                except Exception:
                    video_path = _sanitize_video_path(str(Path("Input") / vp))
            if not video_path.exists() or not video_path.is_file():
                raise HTTPException(status_code=400, detail=f"video_path does not exist: {vp}")
            mode = str(it.get("mode") or (preset.get("mode") if preset else "medium"))
            device = str(it.get("device") or (preset.get("device") if preset else "auto"))
            src_lang = str(it.get("src_lang") or (preset.get("src_lang") if preset else "ja"))
            tgt_lang = str(it.get("tgt_lang") or (preset.get("tgt_lang") if preset else "en"))
            pg = str(it.get("pg") or "off")
            pg_policy_path = str(it.get("pg_policy_path") or "")
            qa = bool(it.get("qa") or False)
            project_name = str(it.get("project") or it.get("project_name") or "")
            style_guide_path = str(it.get("style_guide_path") or "")
            # project output folder stored in runtime; validated here
            if project and project.get("output_subdir"):
                project["output_subdir"] = _sanitize_output_subdir(
                    str(project.get("output_subdir") or "")
                )
            created_ids.append(
                await _submit_one(
                    video_path=video_path,
                    mode=mode,
                    device=device,
                    src_lang=src_lang,
                    tgt_lang=tgt_lang,
                    preset=preset,
                    project=project,
                    pg=pg,
                    pg_policy_path=pg_policy_path,
                    qa=qa,
                    project_name=project_name,
                    style_guide_path=style_guide_path,
                )
            )
    else:
        form = await request.form()
        preset_id = str(form.get("preset_id") or "").strip()
        project_id = str(form.get("project_id") or "").strip()
        preset = store.get_preset(preset_id) if preset_id else None
        project = store.get_project(project_id) if project_id else None
        if project and project.get("output_subdir"):
            project["output_subdir"] = _sanitize_output_subdir(
                str(project.get("output_subdir") or "")
            )
        mode = str(form.get("mode") or (preset.get("mode") if preset else "medium"))
        device = str(form.get("device") or (preset.get("device") if preset else "auto"))
        src_lang = str(form.get("src_lang") or (preset.get("src_lang") if preset else "ja"))
        tgt_lang = str(form.get("tgt_lang") or (preset.get("tgt_lang") if preset else "en"))
        pg = str(form.get("pg") or "off")
        pg_policy_path = str(form.get("pg_policy_path") or "")
        qa = str(form.get("qa") or "").strip() not in {"", "0", "false", "off"}

        files = form.getlist("files") if hasattr(form, "getlist") else []
        if not files:
            raise HTTPException(status_code=400, detail="Provide files")

        up_dir = _input_uploads_dir()
        up_dir.mkdir(parents=True, exist_ok=True)
        for upload in files:
            ctype_u = (getattr(upload, "content_type", None) or "").lower().strip()
            if ctype_u and ctype_u not in _ALLOWED_UPLOAD_MIME:
                raise HTTPException(
                    status_code=400, detail=f"Unsupported upload content-type: {ctype_u}"
                )
            ext = ".mp4"
            name = getattr(upload, "filename", "") or ""
            if "." in name:
                ext = "." + name.rsplit(".", 1)[-1][:8]
            tmp_id = new_id()
            dest = up_dir / f"{tmp_id}{ext}"
            max_bytes = int(limits.max_upload_mb) * 1024 * 1024
            written = 0
            with dest.open("wb") as f:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        with suppress(Exception):
                            dest.unlink(missing_ok=True)
                        raise HTTPException(
                            status_code=400, detail=f"Upload too large (>{limits.max_upload_mb}MB)"
                        )
                    f.write(chunk)
            created_ids.append(
                await _submit_one(
                    video_path=dest,
                    mode=mode,
                    device=device,
                    src_lang=src_lang,
                    tgt_lang=tgt_lang,
                    preset=preset,
                    project=project,
                    pg=pg,
                    pg_policy_path=pg_policy_path,
                    qa=qa,
                )
            )

    return {"ids": created_ids}


@router.get("/api/jobs")
async def list_jobs(
    request: Request,
    state: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 25,
    offset: int = 0,
    _: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    st = status or state
    limit_i = max(1, min(200, int(limit)))
    offset_i = max(0, int(offset))
    jobs_all = store.list(limit=1000, state=st)
    if q:
        qq = str(q).lower().strip()
        if qq:
            jobs_all = [
                j for j in jobs_all if (qq in j.id.lower()) or (qq in (j.video_path or "").lower())
            ]
    total = len(jobs_all)
    jobs = jobs_all[offset_i : offset_i + limit_i]
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
                "mode": j.mode,
                "src_lang": j.src_lang,
                "tgt_lang": j.tgt_lang,
                "device": j.device,
                "runtime": j.runtime,
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


@router.get("/api/jobs/{id}")
async def get_job(
    request: Request, id: str, _: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    d = job.to_dict()
    # Attach checkpoint (best-effort) for stage breakdown.
    with suppress(Exception):
        from anime_v2.jobs.checkpoint import read_ckpt

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


@router.post("/api/jobs/{id}/cancel")
async def cancel_job(
    request: Request, id: str, _: Identity = Depends(require_scope("submit:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    queue = _get_queue(request)
    await queue.cancel(id)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    return job.to_dict()


@router.post("/api/jobs/{id}/pause")
async def pause_job(
    request: Request, id: str, _: Identity = Depends(require_scope("submit:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    queue = _get_queue(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    if job.state != JobState.QUEUED:
        raise HTTPException(status_code=409, detail="Can only pause QUEUED jobs")
    j2 = await queue.pause(id)
    if j2 is None:
        raise HTTPException(status_code=404, detail="Not found")
    return j2.to_dict()


@router.post("/api/jobs/{id}/resume")
async def resume_job(
    request: Request, id: str, _: Identity = Depends(require_scope("submit:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    queue = _get_queue(request)
    scheduler = _get_scheduler(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    if job.state != JobState.PAUSED:
        raise HTTPException(status_code=409, detail="Can only resume PAUSED jobs")
    j2 = await queue.resume(id)
    if j2 is None:
        raise HTTPException(status_code=404, detail="Not found")
    # re-submit to scheduler (best-effort)
    with suppress(Exception):
        scheduler.submit(
            JobRecord(
                job_id=id, mode=j2.mode, device_pref=j2.device, created_at=time.time(), priority=100
            )
        )
    return j2.to_dict()


@router.get("/api/jobs/events")
async def jobs_events(request: Request, _: Identity = Depends(require_scope("read:job"))):
    store = _get_store(request)

    async def gen():
        last: dict[str, str] = {}
        while True:
            if await request.is_disconnected():
                return
            jobs = store.list(limit=200)
            for j in jobs:
                key = f"{j.state.value}:{j.updated_at}:{j.progress:.4f}:{j.message}"
                if last.get(j.id) == key:
                    continue
                last[j.id] = key
                payload = {
                    "id": j.id,
                    "state": j.state.value,
                    "progress": float(j.progress),
                    "message": j.message,
                    "updated_at": j.updated_at,
                    "created_at": j.created_at,
                    "video_path": j.video_path,
                    "mode": j.mode,
                    "src_lang": j.src_lang,
                    "tgt_lang": j.tgt_lang,
                }
                yield {"event": "job", "data": json.dumps(payload)}
            await asyncio.sleep(0.75)

    import json

    return EventSourceResponse(gen())


# --- presets ---
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


# --- projects ---
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


@router.get("/api/jobs/{id}/logs/tail")
async def tail_logs(
    request: Request, id: str, n: int = 200, _: Identity = Depends(require_scope("read:job"))
) -> PlainTextResponse:
    store = _get_store(request)
    return PlainTextResponse(store.tail_log(id, n=n))


@router.get("/api/jobs/{id}/logs/stream")
async def stream_logs(request: Request, id: str, _: Identity = Depends(require_scope("read:job"))):
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
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
        while True:
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

    return EventSourceResponse(gen())


@router.get("/api/jobs/{id}/characters")
async def get_job_characters(
    request: Request, id: str, _: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    rt = dict(job.runtime or {})
    items = rt.get("voice_map", [])
    if not isinstance(items, list):
        items = []
    return {"items": items}


@router.put("/api/jobs/{id}/characters")
async def put_job_characters(
    request: Request, id: str, _: Identity = Depends(require_scope("submit:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")

    ctype = (request.headers.get("content-type") or "").lower()
    items: list[dict[str, Any]] = []
    wav_upload: tuple[str, Any] | None = None  # (character_id, UploadFile)

    if "application/json" in ctype:
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get("items"), list):
            items = [dict(x) for x in body.get("items", []) if isinstance(x, dict)]
        else:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
    else:
        # multipart: allow `data` JSON + optional wav upload for one character
        form = await request.form()
        raw = form.get("data")
        if raw:
            import json as _json

            try:
                data = _json.loads(str(raw))
            except Exception:
                data = {}
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                items = [dict(x) for x in data.get("items", []) if isinstance(x, dict)]
        cid = str(form.get("character_id") or "").strip()
        up = form.get("tts_speaker_wav")
        if cid and up is not None:
            wav_upload = (cid, up)

    # Persist uploaded wav (best-effort)
    if wav_upload:
        cid, upload = wav_upload
        try:
            base_dir = (
                Path(job.work_dir).resolve() if job.work_dir else (_output_root() / id).resolve()
            )
            voices_dir = (base_dir / "voices").resolve()
            voices_dir.mkdir(parents=True, exist_ok=True)
            dest = voices_dir / f"{cid}.wav"
            # UploadFile-like: async read
            written = 0
            with dest.open("wb") as f:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > 50 * 1024 * 1024:
                        raise HTTPException(status_code=400, detail="Speaker WAV too large")
                    f.write(chunk)
            # Update matching item in mapping
            for it in items:
                if str(it.get("character_id") or "") == cid:
                    it["tts_speaker_wav"] = str(dest)
                    it["speaker_strategy"] = "zero-shot"
        except HTTPException:
            raise
        except Exception:
            ...

    rt = dict(job.runtime or {})
    rt["voice_map"] = items
    store.update(id, runtime=rt)
    return {"ok": True, "items": items}


@router.get("/api/jobs/{id}/transcript")
async def get_job_transcript(
    request: Request,
    id: str,
    page: int = 1,
    per_page: int = 50,
    _: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)
    stem = Path(job.video_path).stem if job.video_path else base_dir.name

    src_srt = base_dir / f"{stem}.srt"
    tgt_srt = base_dir / f"{stem}.translated.srt"
    # If no translated SRT yet, fall back to src.
    if not tgt_srt.exists():
        tgt_srt = src_srt

    src = _parse_srt(src_srt)
    tgt = _parse_srt(tgt_srt)

    # Align by index
    n = max(len(src), len(tgt))
    items = []

    st = _load_transcript_store(base_dir)
    seg_over = st.get("segments", {})
    version = int(st.get("version") or 0)

    for i in range(n):
        s0 = (
            src[i]
            if i < len(src)
            else (tgt[i] if i < len(tgt) else {"start": 0.0, "end": 0.0, "text": ""})
        )
        t0 = (
            tgt[i]
            if i < len(tgt)
            else (src[i] if i < len(src) else {"start": 0.0, "end": 0.0, "text": ""})
        )
        ov = seg_over.get(str(i + 1), {}) if isinstance(seg_over, dict) else {}
        tgt_text = str(
            ov.get("tgt_text")
            if isinstance(ov, dict) and "tgt_text" in ov
            else t0.get("text") or ""
        )
        approved = bool(ov.get("approved")) if isinstance(ov, dict) else False
        flags = ov.get("flags") if isinstance(ov, dict) else []
        if not isinstance(flags, list):
            flags = []
        items.append(
            {
                "index": i + 1,
                "start": _fmt_ts_srt(float(s0.get("start", 0.0))),
                "end": _fmt_ts_srt(float(s0.get("end", 0.0))),
                "src_text": str(s0.get("text") or ""),
                "tgt_text": tgt_text,
                "approved": approved,
                "flags": [str(x) for x in flags],
            }
        )

    per = max(1, min(200, int(per_page)))
    p = max(1, int(page))
    total = len(items)
    start_i = (p - 1) * per
    page_items = items[start_i : start_i + per]
    return {"items": page_items, "page": p, "per_page": per, "total": total, "version": version}


@router.put("/api/jobs/{id}/transcript")
async def put_job_transcript(
    request: Request, id: str, _: Identity = Depends(require_scope("submit:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)

    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("updates"), list):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    updates = [u for u in body.get("updates", []) if isinstance(u, dict)]
    if not updates:
        return {"ok": True, "version": int(_load_transcript_store(base_dir).get("version") or 0)}

    st = _load_transcript_store(base_dir)
    segs = st.get("segments", {})
    if not isinstance(segs, dict):
        segs = {}
        st["segments"] = segs

    applied = []
    for u in updates:
        try:
            idx = int(u.get("index"))
            if idx <= 0:
                continue
        except Exception:
            continue
        rec = segs.get(str(idx), {})
        if not isinstance(rec, dict):
            rec = {}
        if "tgt_text" in u:
            rec["tgt_text"] = str(u.get("tgt_text") or "")
        if "approved" in u:
            rec["approved"] = bool(u.get("approved"))
        if "flags" in u:
            flags = u.get("flags")
            if isinstance(flags, list):
                rec["flags"] = [str(x) for x in flags]
        segs[str(idx)] = rec
        applied.append(
            {
                "index": idx,
                "tgt_text": rec.get("tgt_text"),
                "approved": rec.get("approved"),
                "flags": rec.get("flags", []),
            }
        )

    st["version"] = int(st.get("version") or 0) + 1
    st["updated_at"] = now_utc()
    _save_transcript_store(base_dir, st)
    _append_transcript_version(
        base_dir, {"version": st["version"], "updated_at": st["updated_at"], "updates": applied}
    )

    # Persist version on job runtime for visibility.
    rt = dict(job.runtime or {})
    rt["transcript_version"] = st["version"]
    store.update(id, runtime=rt)
    return {"ok": True, "version": st["version"]}


@router.post("/api/jobs/{id}/transcript/synthesize")
async def synthesize_from_approved(
    request: Request, id: str, _: Identity = Depends(require_scope("submit:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    scheduler = _get_scheduler(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
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
    with suppress(Exception):
        scheduler.submit(
            JobRecord(
                job_id=id,
                mode=(job2.mode if job2 else job.mode),
                device_pref=(job2.device if job2 else job.device),
                created_at=time.time(),
                priority=50,
            )
        )
    return {"ok": True}


@router.get("/api/jobs/{id}/review/segments")
async def get_job_review_segments(
    request: Request, id: str, _: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)
    rsp = _review_state_path(base_dir)
    if not rsp.exists():
        try:
            from anime_v2.review.ops import init_review

            init_review(base_dir, video_path=Path(job.video_path) if job.video_path else None)
        except Exception as ex:
            raise HTTPException(status_code=400, detail=f"review init failed: {ex}") from ex

    from anime_v2.review.state import load_state

    return load_state(base_dir)


@router.post("/api/jobs/{id}/review/segments/{segment_id}/edit")
async def post_job_review_edit(
    request: Request,
    id: str,
    segment_id: int,
    _: Identity = Depends(require_scope("submit:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)
    body = await request.json()
    text = str(body.get("text") or "")
    from anime_v2.review.ops import edit_segment

    try:
        edit_segment(base_dir, int(segment_id), text=text)
        return {"ok": True}
    except Exception as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex


@router.post("/api/jobs/{id}/review/segments/{segment_id}/regen")
async def post_job_review_regen(
    request: Request,
    id: str,
    segment_id: int,
    _: Identity = Depends(require_scope("submit:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)
    from anime_v2.review.ops import regen_segment

    try:
        p = regen_segment(base_dir, int(segment_id))
        return {"ok": True, "audio_path": str(p)}
    except Exception as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex


@router.post("/api/jobs/{id}/review/segments/{segment_id}/lock")
async def post_job_review_lock(
    request: Request,
    id: str,
    segment_id: int,
    _: Identity = Depends(require_scope("submit:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)
    from anime_v2.review.ops import lock_segment

    try:
        lock_segment(base_dir, int(segment_id))
        return {"ok": True}
    except Exception as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex


@router.post("/api/jobs/{id}/review/segments/{segment_id}/unlock")
async def post_job_review_unlock(
    request: Request,
    id: str,
    segment_id: int,
    _: Identity = Depends(require_scope("submit:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)
    from anime_v2.review.ops import unlock_segment

    try:
        unlock_segment(base_dir, int(segment_id))
        return {"ok": True}
    except Exception as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex


@router.get("/api/jobs/{id}/review/segments/{segment_id}/audio")
async def get_job_review_audio(
    request: Request,
    id: str,
    segment_id: int,
    _: Identity = Depends(require_scope("read:job")),
) -> Response:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)
    p = _review_audio_path(base_dir, int(segment_id))
    if p is None:
        raise HTTPException(status_code=404, detail="audio not found")
    return _file_range_response(request, p, media_type="audio/wav")


@router.get("/api/jobs/{id}/files")
async def job_files(
    request: Request, id: str, _: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)
    stem = Path(job.video_path).stem if job.video_path else base_dir.name

    # candidates
    mp4 = None
    mkv = None
    hls = None
    lipsync = None
    qa_summary = None
    qa_top_md = None

    for cand in [
        base_dir / f"{stem}.dub.mp4",
        base_dir / "dub.mp4",
        *list(base_dir.glob("*.dub.mp4")),
    ]:
        if cand.exists():
            mp4 = cand
            break
    for cand in [
        base_dir / "final_lipsynced.mp4",
        base_dir / f"{stem}.final_lipsynced.mp4",
        *list(base_dir.glob("*lipsync*.mp4")),
    ]:
        if cand.exists():
            lipsync = cand
            break
    for cand in [
        base_dir / f"{stem}.dub.mkv",
        base_dir / "dub.mkv",
        *list(base_dir.glob("*.dub.mkv")),
    ]:
        if cand.exists():
            mkv = cand
            break

    # HLS: look for master.m3u8 under base_dir (small tree)
    try:
        for cand in base_dir.rglob("master.m3u8"):
            if cand.is_file():
                hls = cand
                break
    except Exception:
        hls = None

    out_root = _output_root()

    def rel_url(p: Path) -> str:
        rel = str(p.resolve().relative_to(out_root)).replace("\\", "/")
        return f"/files/{rel}"

    files: list[dict[str, Any]] = []
    for kind, p in [("hls_manifest", hls), ("lipsync_mp4", lipsync), ("mp4", mp4), ("mkv", mkv)]:
        if p is None:
            continue
        try:
            st = p.stat()
            files.append(
                {
                    "kind": kind,
                    "name": p.name,
                    "path": str(p),
                    "url": rel_url(p),
                    "size_bytes": int(st.st_size),
                    "mtime": float(st.st_mtime),
                }
            )
        except Exception:
            continue

    # Multi-track artifacts (best-effort)
    try:
        tracks_dir = base_dir / "audio" / "tracks"
        if tracks_dir.exists():
            preferred = [
                tracks_dir / "original_full.wav",
                tracks_dir / "dubbed_full.wav",
                tracks_dir / "background_only.wav",
                tracks_dir / "dialogue_only.wav",
                tracks_dir / "original_full.m4a",
                tracks_dir / "dubbed_full.m4a",
                tracks_dir / "background_only.m4a",
                tracks_dir / "dialogue_only.m4a",
            ]
            for p in preferred:
                if not p.exists():
                    continue
                try:
                    st = p.stat()
                    files.append(
                        {
                            "kind": "audio_track",
                            "name": p.name,
                            "path": str(p),
                            "url": rel_url(p),
                            "size_bytes": int(st.st_size),
                            "mtime": float(st.st_mtime),
                        }
                    )
                except Exception:
                    continue
    except Exception:
        pass

    data: dict[str, Any] = {
        "files": files,
        "hls_manifest": None,
        "lipsync_mp4": None,
        "mp4": None,
        "mkv": None,
        "qa_summary": None,
        "qa_top_issues": None,
    }
    if hls is not None:
        data["hls_manifest"] = {"url": rel_url(hls), "path": str(hls)}
    if mp4 is not None:
        data["mp4"] = {"url": rel_url(mp4), "path": str(mp4)}
    if lipsync is not None:
        data["lipsync_mp4"] = {"url": rel_url(lipsync), "path": str(lipsync)}
    if mkv is not None:
        data["mkv"] = {"url": rel_url(mkv), "path": str(mkv)}

    # QA artifacts (best-effort)
    try:
        cand = base_dir / "qa" / "summary.json"
        if cand.exists():
            qa_summary = cand
        cand2 = base_dir / "qa" / "top_issues.md"
        if cand2.exists():
            qa_top_md = cand2
    except Exception:
        qa_summary = None
        qa_top_md = None
    if qa_summary is not None:
        data["qa_summary"] = {"url": rel_url(qa_summary), "path": str(qa_summary)}
    if qa_top_md is not None:
        data["qa_top_issues"] = {"url": rel_url(qa_top_md), "path": str(qa_top_md)}
    return data


@router.get("/api/jobs/{id}/stream/manifest")
async def job_stream_manifest(
    request: Request, id: str, _: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)
    p = _stream_manifest_path(base_dir)
    if not p.exists():
        raise HTTPException(status_code=404, detail="stream manifest not found")
    from anime_v2.utils.io import read_json

    data = read_json(p, default={})
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="invalid manifest")
    return data


@router.get("/api/jobs/{id}/stream/chunks/{chunk_idx}")
async def job_stream_chunk(
    request: Request,
    id: str,
    chunk_idx: int,
    _: Identity = Depends(require_scope("read:job")),
) -> Response:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)
    p = _stream_chunk_mp4_path(base_dir, int(chunk_idx))
    if p is None:
        raise HTTPException(status_code=404, detail="chunk not found")
    return _file_range_response(request, p, media_type="video/mp4")


@router.get("/api/jobs/{id}/qrcode")
async def job_qrcode(request: Request, id: str, _: Identity = Depends(require_scope("read:job"))):
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    # Absolute URL to the UI job page.
    base = str(request.base_url).rstrip("/")
    url = f"{base}/ui/jobs/{id}"
    try:
        import qrcode  # type: ignore
    except Exception as ex:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"qrcode unavailable: {ex}") from ex
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    from fastapi.responses import Response as _Resp

    return _Resp(content=buf.getvalue(), media_type="image/png")


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
