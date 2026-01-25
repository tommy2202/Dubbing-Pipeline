from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, now_utc
from dubbing_pipeline.runtime.scheduler import Scheduler
from dubbing_pipeline.utils.ffmpeg_safe import ffprobe_media_info
from dubbing_pipeline.utils.net import get_client_ip
from dubbing_pipeline.utils.ratelimit import RateLimiter

_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")
_ALLOWED_UPLOAD_MIME = {
    "video/mp4",
    "video/quicktime",
    "video/x-matroska",
    "video/webm",
    "application/octet-stream",  # some browsers
}
_ALLOWED_UPLOAD_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".m4v"}
_ALLOWED_CONTAINER_TOKENS = {
    # MP4/QuickTime family
    "mov",
    "mp4",
    "m4a",
    "3gp",
    "3g2",
    "mj2",
    # Matroska/WebM
    "matroska",
    "webm",
}

# --- series voice store helpers (character voices) ---
_CHAR_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify_character(text: str) -> str:
    t = str(text or "").strip().lower()
    t = _CHAR_SLUG_RE.sub("-", t)
    t = re.sub(r"-{2,}", "-", t).strip("-")
    return t

# Upload session locks (per upload_id) for concurrent chunk writes
_UPLOAD_LOCKS: dict[str, asyncio.Lock] = {}


def _now_iso() -> str:
    return now_utc()


def _new_short_id(prefix: str = "p_") -> str:
    import secrets

    return prefix + secrets.token_hex(8)


def _upload_lock(upload_id: str) -> asyncio.Lock:
    k = str(upload_id or "")
    if not k:
        # fallback, should not happen
        return asyncio.Lock()
    lk = _UPLOAD_LOCKS.get(k)
    if lk is None:
        lk = asyncio.Lock()
        _UPLOAD_LOCKS[k] = lk
    return lk


def _safe_filename(name: str) -> str:
    base = Path(str(name or "")).name.strip() or "upload.mp4"
    base = base.replace("\x00", "")
    # Keep it simple; allow common chars.
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    if len(base) > 160:
        base = base[:160]
    return base


def _client_ip_for_limits(request: Request) -> str:
    """
    Proxy-safe client IP for rate limiting.
    Only trusts forwarded headers when peer is a trusted proxy.
    """
    return get_client_ip(request)


def _enforce_rate_limit(request: Request, *, key: str, limit: int, per_seconds: int) -> None:
    rl: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if rl is None:
        rl = RateLimiter()
        request.app.state.rate_limiter = rl
    if not rl.allow(str(key), limit=int(limit), per_seconds=int(per_seconds)):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")


def _sha256_hex(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _validate_media_or_400(path: Path, *, limits) -> float:
    """
    Validate a media file using ffprobe:
      - container allowlist (format_name tokens)
      - duration bounds
      - optional resolution caps
    Returns duration_s.
    """
    info = ffprobe_media_info(path, timeout_s=20)
    fmt = str(info.get("format_name") or "").strip().lower()
    tokens = {t.strip() for t in fmt.split(",") if t.strip()}
    if not tokens or tokens.isdisjoint(_ALLOWED_CONTAINER_TOKENS):
        raise HTTPException(status_code=400, detail="Invalid media file (unsupported container)")
    dur = float(info.get("duration_s") or 0.0)
    if dur <= 0.5:
        raise HTTPException(status_code=400, detail="Video duration is too short or unreadable")
    if dur > float(limits.max_video_min) * 60.0:
        raise HTTPException(
            status_code=400, detail=f"Video too long (> {limits.max_video_min} minutes)"
        )

    w = int(info.get("width") or 0)
    h = int(info.get("height") or 0)
    if int(getattr(limits, "max_video_width", 0) or 0) > 0 and w > int(limits.max_video_width):
        raise HTTPException(
            status_code=400, detail=f"Video width too large (> {int(limits.max_video_width)}px)"
        )
    if int(getattr(limits, "max_video_height", 0) or 0) > 0 and h > int(limits.max_video_height):
        raise HTTPException(
            status_code=400, detail=f"Video height too large (> {int(limits.max_video_height)}px)"
        )
    if (
        int(getattr(limits, "max_video_pixels", 0) or 0) > 0
        and w > 0
        and h > 0
        and (int(w) * int(h) > int(limits.max_video_pixels))
    ):
        raise HTTPException(status_code=400, detail="Video resolution too large")

    return float(dur)


def _app_root() -> Path:
    # CI compatibility: many tests and docs use `/workspace` as a stable example path.
    # On some CI runners, the repo checkout is not actually mounted at `/workspace`.
    # If APP_ROOT is set to `/workspace` but that directory doesn't exist, treat the
    # current working directory as the effective app root.
    p = Path(get_settings().app_root).resolve()
    if str(p) == "/workspace" and not p.exists():
        return Path.cwd().resolve()
    return p


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


def _input_imports_dir() -> Path:
    """
    Directory where the web UI/API stores imported subtitles/transcripts.
    Not served publicly.
    """
    return (_input_dir() / "imports").resolve()


def _sanitize_video_path(p: str) -> Path:
    if not p or not _SAFE_PATH_RE.match(p):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid video_path")

    raw = Path(p)
    resolved = raw.resolve() if raw.is_absolute() else (_app_root() / raw).resolve()

    # CI compatibility: some environments check out the repo under a non-/workspace path.
    # Tests (and some docs/scripts) historically reference absolute `/workspace/...` paths.
    # If `/workspace` does not exist on this host, treat it as an example for APP_ROOT.
    if raw.is_absolute() and not resolved.exists():
        try:
            if not Path("/workspace").exists() and str(raw).startswith("/workspace/"):
                resolved = (_app_root() / raw.relative_to("/workspace")).resolve()
        except Exception:
            pass

    # File inputs are allowlisted to INPUT_DIR only (prevents arbitrary reads under APP_ROOT).
    inp = _input_dir().resolve()
    try:
        resolved.relative_to(inp)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="video_path must be under INPUT_DIR"
        ) from None
    # Uploaded files must be referenced via upload_id (prevents cross-user access via server file picker).
    up = _input_uploads_dir().resolve()
    try:
        resolved.relative_to(up)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="video_path cannot be under INPUT_UPLOADS_DIR; use upload_id",
        )
    except HTTPException:
        raise
    except Exception:
        pass
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


def _file_range_response(
    request: Request,
    path: Path,
    *,
    media_type: str,
    allowed_roots: list[Path] | None = None,
) -> Response:
    """
    Minimal HTTP Range support for previews.
    Streams from disk to avoid loading full files into memory.
    """
    p = Path(path).resolve()
    if allowed_roots:
        ok = False
        for root in allowed_roots:
            try:
                p.relative_to(Path(root).resolve())
                ok = True
                break
            except Exception:
                continue
        if not ok:
            raise HTTPException(status_code=404, detail="Not found")
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    size = p.stat().st_size
    rng = (request.headers.get("range") or "").strip().lower()

    def _iter_range(start: int, end: int):
        with p.open("rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    if not rng:
        headers = {"Accept-Ranges": "bytes", "Content-Length": str(size)}
        return StreamingResponse(_iter_range(0, max(0, size - 1)), media_type=media_type, headers=headers)

    m = re.match(r"bytes=(\d*)-(\d*)", rng)
    if not m:
        headers = {"Accept-Ranges": "bytes", "Content-Length": str(size)}
        return StreamingResponse(_iter_range(0, max(0, size - 1)), media_type=media_type, headers=headers)

    start_s, end_s = m.group(1), m.group(2)
    if not start_s and not end_s:
        headers = {"Accept-Ranges": "bytes", "Content-Length": str(size)}
        return StreamingResponse(_iter_range(0, max(0, size - 1)), media_type=media_type, headers=headers)

    if start_s:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    else:
        # Suffix range: bytes=-N
        suffix = int(end_s or 0)
        if suffix <= 0:
            headers = {"Accept-Ranges": "bytes", "Content-Length": str(size)}
            return StreamingResponse(
                _iter_range(0, max(0, size - 1)), media_type=media_type, headers=headers
            )
        start = max(0, size - suffix)
        end = size - 1

    if start >= size:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{size}"},
            media_type=media_type,
        )
    end = max(start, min(end, size - 1))

    headers = {
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
    }
    return StreamingResponse(
        _iter_range(start, end),
        status_code=206,
        media_type=media_type,
        headers=headers,
    )


def _stream_manifest_path(base_dir: Path) -> Path:
    return (base_dir / "stream" / "manifest.json").resolve()


def _stream_chunk_mp4_path(base_dir: Path, idx: int) -> Path | None:
    """
    idx is 1-based chunk index.
    """
    p = (base_dir / "stream" / f"chunk_{int(idx):03d}.mp4").resolve()
    return p if p.exists() else None


def _job_base_dir(job: Job) -> Path:
    # Canonical Output/ layout (single source of truth).
    try:
        from dubbing_pipeline.library.paths import get_job_output_root

        return get_job_output_root(job)
    except Exception:
        # Conservative fallback
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


def _parse_iso_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return float(dt.timestamp())
    except Exception:
        return None


def _transcript_store_paths(base_dir: Path) -> tuple[Path, Path]:
    return base_dir / "transcript_store.json", base_dir / "transcript_versions.jsonl"


def _load_transcript_store(base_dir: Path) -> dict[str, Any]:
    store_path, _ = _transcript_store_paths(base_dir)
    if not store_path.exists():
        return {"version": 0, "segments": {}}
    with suppress(Exception):
        data = json.loads(store_path.read_text(encoding="utf-8", errors="replace"))
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
    tmp = store_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(store_path)


def _append_transcript_version(base_dir: Path, entry: dict[str, Any]) -> None:
    _, vpath = _transcript_store_paths(base_dir)
    vpath.parent.mkdir(parents=True, exist_ok=True)
    with vpath.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def _apply_transcript_updates(
    *, base_dir: Path, updates: list[dict[str, Any]]
) -> tuple[int, list[dict[str, Any]]]:
    if not updates:
        st = _load_transcript_store(base_dir)
        return int(st.get("version") or 0), []
    st = _load_transcript_store(base_dir)
    segs = st.get("segments", {})
    if not isinstance(segs, dict):
        segs = {}
        st["segments"] = segs

    applied: list[dict[str, Any]] = []
    for u in updates:
        if not isinstance(u, dict):
            continue
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

    if not applied:
        return int(st.get("version") or 0), []
    st["version"] = int(st.get("version") or 0) + 1
    st["updated_at"] = now_utc()
    _save_transcript_store(base_dir, st)
    _append_transcript_version(
        base_dir, {"version": st["version"], "updated_at": st["updated_at"], "updates": applied}
    )
    return int(st.get("version") or 0), applied


def _voice_refs_manifest_path(base_dir: Path) -> Path:
    return (base_dir / "analysis" / "voice_refs" / "manifest.json").resolve()


def _voice_ref_override_path(base_dir: Path, speaker_id: str) -> Path:
    # Keep overrides job-local (do not mutate global voice store by default).
    safe = Path(str(speaker_id or "")).name.strip()
    return (base_dir / "analysis" / "voice_refs" / f"{safe}.wav").resolve()


def _voice_ref_allowed_roots(base_dir: Path) -> list[Path]:
    s = get_settings()
    roots = [Path(base_dir).resolve()]
    with suppress(Exception):
        roots.append(Path(s.voice_store_dir).resolve())
    return roots


def _voice_ref_audio_path_from_manifest(
    *,
    base_dir: Path,
    speaker_id: str,
    manifest: dict[str, Any],
) -> Path | None:
    sid = Path(str(speaker_id or "")).name.strip()
    if not sid:
        return None
    # Prefer per-job override if present.
    ov = _voice_ref_override_path(base_dir, sid)
    if ov.exists() and ov.is_file():
        return ov
    items = manifest.get("items") if isinstance(manifest, dict) else None
    if not isinstance(items, dict):
        return None
    rec = items.get(sid)
    if not isinstance(rec, dict):
        return None
    # Prefer job-local exported ref when present.
    p0 = str(rec.get("job_ref_path") or "").strip()
    if p0:
        p = Path(p0).resolve()
    else:
        p = Path(str(rec.get("ref_path") or "")).resolve()
    if not p.exists() or not p.is_file():
        return None
    # Prevent arbitrary file reads: ref must be under allowed roots.
    for root in _voice_ref_allowed_roots(base_dir):
        try:
            p.relative_to(root)
            return p
        except Exception:
            continue
    return None


def _privacy_blocks_voice_refs(job: Job) -> bool:
    try:
        rt = dict(job.runtime or {})
    except Exception:
        rt = {}
    # Conservative: if privacy/minimal artifacts, do not serve extracted reference audio.
    pm = rt.get("privacy_mode")
    if pm in {"on", "1", True}:
        return True
    if bool(rt.get("minimal_artifacts") or False):
        return True
    return False
