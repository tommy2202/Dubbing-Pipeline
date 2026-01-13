from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import json
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
from anime_v2.api.middleware import audit_event
from anime_v2.api.models import AuthStore, Role
from anime_v2.api.security import decode_token
from anime_v2.config import get_settings
from anime_v2.jobs.limits import concurrent_jobs_for_user, get_limits, used_minutes_today
from anime_v2.jobs.models import Job, JobState, new_id, now_utc
from anime_v2.ops.metrics import jobs_queued, pipeline_job_total
from anime_v2.ops.storage import ensure_free_space
from anime_v2.runtime import lifecycle
from anime_v2.runtime.scheduler import JobRecord, Scheduler
from anime_v2.security.crypto import (
    CryptoConfigError,
    encrypt_file,
    encryption_enabled_for,
    is_encrypted_path,
    materialize_decrypted,
)
from anime_v2.utils.crypto import verify_secret
from anime_v2.utils.ffmpeg_safe import FFmpegError, ffprobe_media_info
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

_MAX_IMPORT_TEXT_BYTES = 2 * 1024 * 1024  # 2MB per imported text file (SRT/JSON)

# Upload session locks (per upload_id) for concurrent chunk writes
_UPLOAD_LOCKS: dict[str, asyncio.Lock] = {}


def _parse_library_metadata_or_422(payload: dict[str, Any]) -> tuple[str, str, int, int]:
    """
    Parse required library metadata for job submission.
    Backwards-compatible for old persisted jobs, but NEW submissions must include:
      - series_title (non-empty)
      - season (parseable int >= 1)
      - episode (parseable int >= 1)
    """
    from anime_v2.library.normalize import normalize_series_title, parse_int_strict, series_to_slug

    series_title = normalize_series_title(str(payload.get("series_title") or ""))
    if not series_title:
        raise HTTPException(status_code=422, detail="series_title is required")
    slug = str(payload.get("series_slug") or "").strip()
    if not slug:
        slug = series_to_slug(series_title)
    if not slug:
        raise HTTPException(status_code=422, detail="series_title is invalid (cannot derive slug)")

    # Accept either *_number or *_text (UI sends text).
    season_in = payload.get("season_number")
    if season_in is None:
        season_in = payload.get("season_text")
    if season_in is None:
        season_in = payload.get("season")
    episode_in = payload.get("episode_number")
    if episode_in is None:
        episode_in = payload.get("episode_text")
    if episode_in is None:
        episode_in = payload.get("episode")

    try:
        season_number = parse_int_strict(season_in, "season_number")
    except ValueError as ex:
        raise HTTPException(status_code=422, detail=str(ex)) from None
    try:
        episode_number = parse_int_strict(episode_in, "episode_number")
    except ValueError as ex:
        raise HTTPException(status_code=422, detail=str(ex)) from None

    return series_title, slug, int(season_number), int(episode_number)


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
    Only trusts forwarded headers in Cloudflare mode when peer is a trusted proxy subnet.
    """
    peer = request.client.host if request.client else ""
    s = get_settings()
    mode = str(getattr(s, "remote_access_mode", "off") or "off").strip().lower()
    trust = bool(getattr(s, "trust_proxy_headers", False)) and mode == "cloudflare"
    if not trust:
        return peer or "unknown"

    # Only accept forwarded headers if the immediate peer is trusted.
    try:
        trusted = str(getattr(s, "trusted_proxy_subnets", "") or "").strip()
        nets = [
            ipaddress.ip_network(x.strip(), strict=False) for x in trusted.split(",") if x.strip()
        ]
        if not nets:
            # conservative: if not configured, do not trust
            return peer or "unknown"
        pip = ipaddress.ip_address(peer) if peer else None
        if pip is None or not any(pip in n for n in nets):
            return peer or "unknown"
    except Exception:
        return peer or "unknown"

    # Cloudflare headers (preferred)
    cf = (request.headers.get("cf-connecting-ip") or "").strip()
    if cf:
        return cf
    # Fall back to X-Forwarded-For (first hop)
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return peer or "unknown"


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


@router.post("/api/uploads/init")
async def uploads_init(
    request: Request, ident: Identity = Depends(require_scope("submit:job"))
) -> dict[str, Any]:
    """
    Initialize a resumable upload session.

    Body JSON:
      - filename: str
      - total_bytes: int
      - mime: str (optional)
    """
    store = _get_store(request)
    limits = get_limits()
    # Moderate: init is lightweight but should not be spammed
    _enforce_rate_limit(
        request,
        key=f"upload:init:user:{ident.user.id}",
        limit=30,
        per_seconds=60,
    )
    _enforce_rate_limit(
        request,
        key=f"upload:init:ip:{_client_ip_for_limits(request)}",
        limit=60,
        per_seconds=60,
    )
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    filename = _safe_filename(str(body.get("filename") or "upload.mp4"))
    ext = Path(filename).suffix.lower()
    if ext not in _ALLOWED_UPLOAD_EXTS:
        raise HTTPException(
            status_code=400, detail=f"Unsupported file extension: {ext or '(none)'}"
        )
    try:
        total = int(body.get("total_bytes") or 0)
    except Exception:
        total = 0
    if total <= 0:
        raise HTTPException(status_code=400, detail="total_bytes required")
    max_bytes = int(limits.max_upload_mb) * 1024 * 1024
    if total > max_bytes:
        raise HTTPException(status_code=400, detail=f"Upload too large (>{limits.max_upload_mb}MB)")
    mime = str(body.get("mime") or "").lower().strip()
    if mime and mime not in _ALLOWED_UPLOAD_MIME:
        raise HTTPException(status_code=400, detail=f"Unsupported upload content-type: {mime}")

    up_dir = _input_uploads_dir()
    up_dir.mkdir(parents=True, exist_ok=True)
    upload_id = _new_short_id("up_")
    part_path = (up_dir / f"{upload_id}.part").resolve()
    final_name = f"{upload_id}_{filename}"
    final_path = (up_dir / final_name).resolve()
    # Ensure under uploads dir
    try:
        part_path.relative_to(up_dir)
        final_path.relative_to(up_dir)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid upload paths") from None

    chunk_bytes = int(get_settings().upload_chunk_bytes)
    chunk_bytes = max(256 * 1024, min(chunk_bytes, 20 * 1024 * 1024))
    rec = {
        "id": upload_id,
        "owner_id": ident.user.id,
        "filename": filename,
        "orig_stem": Path(filename).stem,
        "total_bytes": int(total),
        "chunk_bytes": int(chunk_bytes),
        "part_path": str(part_path),
        "final_path": str(final_path),
        "received": {},  # idx -> {offset,size,sha256}
        "received_bytes": 0,
        "completed": False,
        "encrypted": False,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    store.put_upload(upload_id, rec)
    audit_event(
        "upload.init",
        request=request,
        user_id=ident.user.id,
        meta={"upload_id": upload_id, "total_bytes": int(total), "filename": filename},
    )
    return {
        "upload_id": upload_id,
        "chunk_bytes": int(chunk_bytes),
        "max_upload_mb": int(limits.max_upload_mb),
    }


@router.get("/api/uploads/{upload_id}")
async def uploads_status(
    request: Request, upload_id: str, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    rec = store.get_upload(upload_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Not found")
    if (
        str(rec.get("owner_id") or "") != str(ident.user.id)
        and str(ident.user.role.value) != "admin"
    ):
        raise HTTPException(status_code=403, detail="Forbidden")
    return {
        "upload_id": str(rec.get("id") or upload_id),
        "total_bytes": int(rec.get("total_bytes") or 0),
        "chunk_bytes": int(rec.get("chunk_bytes") or 0),
        "received_bytes": int(rec.get("received_bytes") or 0),
        "completed": bool(rec.get("completed")),
        "received": rec.get("received") if isinstance(rec.get("received"), dict) else {},
    }


@router.post("/api/uploads/{upload_id}/chunk")
async def uploads_chunk(
    request: Request,
    upload_id: str,
    index: int,
    offset: int,
    ident: Identity = Depends(require_scope("submit:job")),
) -> dict[str, Any]:
    """
    Upload a chunk (idempotent when index+sha match).

    Query params:
      - index: int (0-based)
      - offset: int (byte offset)
    Headers:
      - X-Chunk-Sha256: hex sha256 of request body (required)
    Body:
      - raw bytes (application/octet-stream)
    """
    store = _get_store(request)
    # Chunking can be noisy; allow sustained uploads but prevent abuse.
    _enforce_rate_limit(
        request,
        key=f"upload:chunk:user:{ident.user.id}",
        limit=600,
        per_seconds=60,
    )
    _enforce_rate_limit(
        request,
        key=f"upload:chunk:ip:{_client_ip_for_limits(request)}",
        limit=1200,
        per_seconds=60,
    )
    rec = store.get_upload(upload_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Not found")
    if (
        str(rec.get("owner_id") or "") != str(ident.user.id)
        and str(ident.user.role.value) != "admin"
    ):
        raise HTTPException(status_code=403, detail="Forbidden")
    if bool(rec.get("completed")):
        return {"ok": True, "already_completed": True}

    total = int(rec.get("total_bytes") or 0)
    part_path = Path(str(rec.get("part_path") or "")).resolve()
    if total <= 0 or not str(part_path):
        raise HTTPException(status_code=400, detail="Invalid upload session")

    body = await request.body()
    sha = (request.headers.get("x-chunk-sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise HTTPException(status_code=400, detail="Missing/invalid X-Chunk-Sha256")
    if _sha256_hex(body) != sha:
        raise HTTPException(status_code=400, detail="Chunk checksum mismatch")

    if offset < 0 or offset >= total:
        raise HTTPException(status_code=400, detail="offset out of bounds")
    if (offset + len(body)) > total:
        raise HTTPException(status_code=400, detail="chunk exceeds total_bytes")
    # Per-chunk size guard (defense-in-depth)
    try:
        max_chunk = int(rec.get("chunk_bytes") or 0)
    except Exception:
        max_chunk = 0
    if max_chunk > 0 and len(body) > (max_chunk + 1024):
        raise HTTPException(status_code=400, detail="chunk too large")

    idx = int(index)
    if idx < 0:
        raise HTTPException(status_code=400, detail="index out of bounds")

    async with _upload_lock(upload_id):
        # reload inside lock
        rec2 = store.get_upload(upload_id) or rec
        received = rec2.get("received")
        if not isinstance(received, dict):
            received = {}

        prev = received.get(str(idx))
        if (
            isinstance(prev, dict)
            and str(prev.get("sha256") or "") == sha
            and int(prev.get("size") or 0) == len(body)
        ):
            # already accepted
            return {
                "ok": True,
                "received_bytes": int(rec2.get("received_bytes") or 0),
                "dedup": True,
            }

        part_path.parent.mkdir(parents=True, exist_ok=True)
        # random-access write
        with part_path.open("r+b" if part_path.exists() else "w+b") as f:
            f.seek(int(offset))
            f.write(body)

        received[str(idx)] = {"offset": int(offset), "size": int(len(body)), "sha256": sha}
        received_bytes = int(rec2.get("received_bytes") or 0)
        received_bytes += int(len(body))
        store.update_upload(
            upload_id,
            received=received,
            received_bytes=int(received_bytes),
            updated_at=_now_iso(),
        )

    # Audit at coarse granularity to avoid massive logs; include index/size only.
    with suppress(Exception):
        audit_event(
            "upload.chunk",
            request=request,
            user_id=ident.user.id,
            meta={"upload_id": upload_id, "index": int(idx), "size": int(len(body))},
        )
    return {"ok": True, "received_bytes": int(received_bytes)}


@router.post("/api/uploads/{upload_id}/complete")
async def uploads_complete(
    request: Request, upload_id: str, ident: Identity = Depends(require_scope("submit:job"))
) -> dict[str, Any]:
    """
    Finalize an upload: optionally verify whole-file sha256, then move to final_path.

    Body JSON:
      - final_sha256: str (optional)
    """
    store = _get_store(request)
    _enforce_rate_limit(
        request,
        key=f"upload:complete:user:{ident.user.id}",
        limit=30,
        per_seconds=60,
    )
    _enforce_rate_limit(
        request,
        key=f"upload:complete:ip:{_client_ip_for_limits(request)}",
        limit=60,
        per_seconds=60,
    )
    rec = store.get_upload(upload_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Not found")
    if (
        str(rec.get("owner_id") or "") != str(ident.user.id)
        and str(ident.user.role.value) != "admin"
    ):
        raise HTTPException(status_code=403, detail="Forbidden")

    body = await request.json()
    if not isinstance(body, dict):
        body = {}
    final_sha = str(body.get("final_sha256") or "").strip().lower()
    if final_sha and not re.fullmatch(r"[0-9a-f]{64}", final_sha):
        raise HTTPException(status_code=400, detail="Invalid final_sha256")

    async with _upload_lock(upload_id):
        rec2 = store.get_upload(upload_id) or rec
        if bool(rec2.get("completed")):
            return {"ok": True, "video_path": str(rec2.get("final_path") or "")}
        total = int(rec2.get("total_bytes") or 0)
        part_path = Path(str(rec2.get("part_path") or "")).resolve()
        final_path = Path(str(rec2.get("final_path") or "")).resolve()
        if total <= 0 or not part_path.exists():
            raise HTTPException(status_code=400, detail="Upload missing data")

        # Verify file size
        st = part_path.stat()
        if int(st.st_size) != int(total):
            raise HTTPException(status_code=400, detail="Upload incomplete (size mismatch)")

        if final_sha:
            # Stream hash (avoid loading into memory)
            h = hashlib.sha256()
            with part_path.open("rb") as f:
                while True:
                    buf = f.read(1024 * 1024)
                    if not buf:
                        break
                    h.update(buf)
            if h.hexdigest() != final_sha:
                raise HTTPException(status_code=400, detail="Final checksum mismatch")

        final_path.parent.mkdir(parents=True, exist_ok=True)
        part_path.replace(final_path)

        # ffprobe validation before optional encryption (reject corrupt/unsupported uploads early).
        try:
            _ = _validate_media_or_400(final_path, limits=get_limits())
        except HTTPException:
            with suppress(Exception):
                final_path.unlink(missing_ok=True)
            raise
        except Exception as ex:
            with suppress(Exception):
                final_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400, detail=f"Invalid media file (ffprobe failed): {ex}"
            ) from ex

        # Optional: encrypt uploads at rest (best-effort, but fail-safe when enabled).
        if encryption_enabled_for("uploads"):
            enc_path = final_path.with_suffix(final_path.suffix + ".enc")
            try:
                encrypt_file(final_path, enc_path, kind="uploads", job_id=None)
            except CryptoConfigError as ex:
                # Fail-safe: do not keep plaintext when encryption is enabled but misconfigured.
                with suppress(Exception):
                    final_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=500, detail="Upload encryption misconfigured"
                ) from ex
            except Exception as ex:
                with suppress(Exception):
                    enc_path.unlink(missing_ok=True)
                with suppress(Exception):
                    final_path.unlink(missing_ok=True)
                raise HTTPException(status_code=500, detail="Upload encryption failed") from ex
            with suppress(Exception):
                final_path.unlink(missing_ok=True)
            final_path = enc_path
            store.update_upload(
                upload_id,
                completed=True,
                final_path=str(final_path),
                encrypted=True,
                updated_at=_now_iso(),
            )
        else:
            store.update_upload(upload_id, completed=True, updated_at=_now_iso())

    audit_event(
        "upload.complete",
        request=request,
        user_id=ident.user.id,
        meta={"upload_id": upload_id, "final_path": str(final_path.name)},
    )
    return {"ok": True, "video_path": str(final_path)}


@router.get("/api/files")
async def list_server_files(
    request: Request,
    dir: str | None = None,
    ident: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    """
    Server-local file picker (reliable fallback).
    Lists only under APP_ROOT/Input by default.
    """
    root = _input_dir().resolve()
    sub = (dir or "").strip().strip("/")
    target = (root / sub).resolve()
    try:
        target.relative_to(root)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid dir") from None
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="Not found")
    items: list[dict[str, Any]] = []
    # only one level; keep it cheap
    for p in sorted(target.iterdir()):
        if p.name.startswith("."):
            continue
        # Do not expose resumable-upload staging directory via the file picker.
        if p.is_dir() and p.name == "uploads":
            continue
        try:
            if p.is_dir():
                items.append(
                    {
                        "type": "dir",
                        "name": p.name,
                        "path": str(p.relative_to(root)).replace("\\", "/"),
                    }
                )
            elif p.is_file():
                if p.suffix.lower() not in {".mp4", ".mkv", ".mov", ".webm", ".m4v"}:
                    continue
                st = p.stat()
                items.append(
                    {
                        "type": "file",
                        "name": p.name,
                        "path": str(p.relative_to(_app_root())).replace("\\", "/"),
                        "size_bytes": int(st.st_size),
                        "mtime": float(st.st_mtime),
                    }
                )
        except Exception:
            continue
        if len(items) >= 200:
            break
    return {
        "root": str(root),
        "dir": str(target.relative_to(root)).replace("\\", "/"),
        "items": items,
    }


def _app_root() -> Path:
    # CI compatibility: many tests and docs use `/workspace` as a stable placeholder path.
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
    # If `/workspace` does not exist on this host, treat it as a placeholder for APP_ROOT.
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
                p = Path(str(s.get("audio_path_current") or "")).resolve()
                # Prevent arbitrary file reads: audio must live under this job's output folder.
                try:
                    p.relative_to(Path(base_dir).resolve())
                except Exception:
                    return None
                return p if p.exists() and p.is_file() else None
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


@router.post("/api/jobs")
async def create_job(
    request: Request, ident: Identity = Depends(require_scope("submit:job"))
) -> dict[str, str]:
    audit_event(
        "job.submit_attempt",
        request=request,
        user_id=ident.user.id,
        meta={"kind": ident.kind},
    )
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
    ip = _client_ip_for_limits(request)
    if not rl.allow(f"jobs:submit:ip:{ip}", limit=25, per_seconds=60):
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
    cache_policy = "full"
    speaker_smoothing = False
    scene_detect = "audio"
    director = False
    director_strength = 0.5
    video_path: Path | None = None
    duration_s = 0.0

    # Required library metadata.
    series_title = ""
    series_slug = ""
    season_number = 0
    episode_number = 0

    upload_id = ""
    upload_stem = ""
    import_src_srt_text: str | None = None
    import_tgt_srt_text: str | None = None
    import_transcript_json_text: str | None = None

    if "application/json" in ctype:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON")
        # Library metadata is required for new jobs (validate early).
        series_title, series_slug, season_number, episode_number = _parse_library_metadata_or_422(
            body
        )
        mode = str(body.get("mode") or mode)
        device = str(body.get("device") or device)
        src_lang = str(body.get("src_lang") or src_lang)
        tgt_lang = str(body.get("tgt_lang") or tgt_lang)
        pg = str(body.get("pg") or pg)
        pg_policy_path = str(body.get("pg_policy_path") or pg_policy_path)
        qa = bool(body.get("qa") or False)
        project_name = str(body.get("project") or body.get("project_name") or "")
        style_guide_path = str(body.get("style_guide_path") or "")
        cache_policy = str(body.get("cache_policy") or cache_policy)
        # Privacy knobs (optional; persisted on job.runtime)
        privacy_mode = str(body.get("privacy") or body.get("privacy_mode") or "").strip()
        no_store_transcript = bool(body.get("no_store_transcript") or False)
        no_store_source_audio = bool(body.get("no_store_source_audio") or False)
        minimal_artifacts = bool(body.get("minimal_artifacts") or False)
        speaker_smoothing = bool(body.get("speaker_smoothing") or False)
        scene_detect = str(body.get("scene_detect") or scene_detect)
        director = bool(body.get("director") or False)
        director_strength = float(body.get("director_strength") or director_strength)
        upload_id = str(body.get("upload_id") or "").strip()
        # Optional imported transcripts (small; allow client to send text to avoid extra upload plumbing).
        if isinstance(body.get("src_srt_text"), str):
            import_src_srt_text = str(body.get("src_srt_text") or "")
        if isinstance(body.get("tgt_srt_text"), str):
            import_tgt_srt_text = str(body.get("tgt_srt_text") or "")
        if isinstance(body.get("transcript_json_text"), str):
            import_transcript_json_text = str(body.get("transcript_json_text") or "")
        if upload_id:
            urec = store.get_upload(upload_id)
            if not urec or not bool(urec.get("completed")):
                raise HTTPException(status_code=400, detail="upload_id not completed")
            vp = str(urec.get("final_path") or "")
            up_root = _input_uploads_dir().resolve()
            video_path = Path(vp).resolve()
            try:
                video_path.relative_to(up_root)
            except Exception:
                raise HTTPException(status_code=400, detail="upload_id path not allowed") from None
            upload_stem = str(urec.get("orig_stem") or "").strip()
        else:
            vp = body.get("video_path")
            if not isinstance(vp, str):
                raise HTTPException(status_code=400, detail="Missing video_path (or upload_id)")
            video_path = _sanitize_video_path(vp)
            if not video_path.exists():
                raise HTTPException(status_code=400, detail="video_path does not exist")
            if not video_path.is_file():
                raise HTTPException(status_code=400, detail="video_path must be a file")
    else:
        # multipart/form-data
        form = parsed_form or await request.form()
        series_title, series_slug, season_number, episode_number = _parse_library_metadata_or_422(
            dict(form)
        )
        mode = str(form.get("mode") or mode)
        device = str(form.get("device") or device)
        src_lang = str(form.get("src_lang") or src_lang)
        tgt_lang = str(form.get("tgt_lang") or tgt_lang)
        pg = str(form.get("pg") or pg)
        pg_policy_path = str(form.get("pg_policy_path") or pg_policy_path)
        qa = str(form.get("qa") or "").strip() not in {"", "0", "false", "off"}
        project_name = str(form.get("project") or form.get("project_name") or "")
        style_guide_path = str(form.get("style_guide_path") or "")
        cache_policy = str(form.get("cache_policy") or cache_policy)
        # Privacy knobs (optional; persisted on job.runtime)
        privacy_mode = str(form.get("privacy") or form.get("privacy_mode") or "").strip()
        no_store_transcript = str(form.get("no_store_transcript") or "").strip() not in {
            "",
            "0",
            "false",
            "off",
        }
        no_store_source_audio = str(form.get("no_store_source_audio") or "").strip() not in {
            "",
            "0",
            "false",
            "off",
        }
        minimal_artifacts = str(form.get("minimal_artifacts") or "").strip() not in {
            "",
            "0",
            "false",
            "off",
        }
        speaker_smoothing = str(form.get("speaker_smoothing") or "").strip() not in {
            "",
            "0",
            "false",
            "off",
        }
        scene_detect = str(form.get("scene_detect") or scene_detect)
        director = str(form.get("director") or "").strip() not in {"", "0", "false", "off"}
        director_strength = float(form.get("director_strength") or director_strength)

        file = form.get("file")
        vp = form.get("video_path")
        upload_id = str(form.get("upload_id") or "").strip()
        # Optional import files (read as text; cap size).
        try:
            srcf = form.get("src_srt")
            if srcf is not None and hasattr(srcf, "read"):
                raw = await srcf.read()
                if raw and len(raw) <= _MAX_IMPORT_TEXT_BYTES:
                    import_src_srt_text = raw.decode("utf-8", errors="replace")
        except Exception:
            import_src_srt_text = None
        try:
            tgtf = form.get("tgt_srt")
            if tgtf is not None and hasattr(tgtf, "read"):
                raw = await tgtf.read()
                if raw and len(raw) <= _MAX_IMPORT_TEXT_BYTES:
                    import_tgt_srt_text = raw.decode("utf-8", errors="replace")
        except Exception:
            import_tgt_srt_text = None
        try:
            jsf = form.get("transcript_json")
            if jsf is not None and hasattr(jsf, "read"):
                raw = await jsf.read()
                if raw and len(raw) <= _MAX_IMPORT_TEXT_BYTES:
                    import_transcript_json_text = raw.decode("utf-8", errors="replace")
        except Exception:
            import_transcript_json_text = None
        if file is None and vp is None and not upload_id:
            raise HTTPException(status_code=400, detail="Provide file, upload_id, or video_path")

        if upload_id:
            urec = store.get_upload(upload_id)
            if not urec or not bool(urec.get("completed")):
                raise HTTPException(status_code=400, detail="upload_id not completed")
            up_root = _input_uploads_dir().resolve()
            video_path = Path(str(urec.get("final_path") or "")).resolve()
            try:
                video_path.relative_to(up_root)
            except Exception:
                raise HTTPException(status_code=400, detail="upload_id path not allowed") from None
            if not video_path.exists():
                raise HTTPException(status_code=400, detail="upload path missing")
            upload_stem = str(urec.get("orig_stem") or "").strip()
        elif vp is not None:
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
            name = getattr(upload, "filename", "") or ""
            ext = (("." + name.rsplit(".", 1)[-1]) if "." in name else ".mp4").lower()[:8]
            if ext not in _ALLOWED_UPLOAD_EXTS:
                raise HTTPException(status_code=400, detail=f"Unsupported file extension: {ext}")
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
    # Extension allowlist (defense-in-depth). Encrypted-at-rest inputs are allowed (validated via ffprobe after decrypt).
    if not is_encrypted_path(video_path) and video_path.suffix.lower() not in _ALLOWED_UPLOAD_EXTS:
        raise HTTPException(status_code=400, detail="Unsupported file type")
    # Validate using ffprobe (no user-controlled args).
    try:
        with materialize_decrypted(video_path, kind="uploads", job_id=None, suffix=".input") as mat:
            duration_s = float(_validate_media_or_400(mat.path, limits=limits))
    except HTTPException:
        raise
    except FFmpegError as ex:
        raise HTTPException(
            status_code=400, detail=f"Invalid media file (ffprobe failed): {ex}"
        ) from ex

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
        series_title=str(series_title),
        series_slug=str(series_slug),
        season_number=int(season_number),
        episode_number=int(episode_number),
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

    # retention policy (per-job)
    cp = str(cache_policy or "").strip().lower()
    if cp in {"full", "balanced", "minimal"}:
        rt["cache_policy"] = cp

    # privacy mode + data minimization (per-job)
    try:
        from anime_v2.security.privacy import resolve_privacy

        priv = resolve_privacy(
            {
                "privacy_mode": str(privacy_mode or ""),
                "no_store_transcript": bool(no_store_transcript),
                "no_store_source_audio": bool(no_store_source_audio),
                "minimal_artifacts": bool(minimal_artifacts),
            }
        )
        rt.update(priv.to_runtime_patch())
        # Privacy triggers retention automation (minimal) unless an explicit cache_policy was supplied.
        if (priv.privacy_on or priv.minimal_artifacts) and "cache_policy" not in rt:
            rt["cache_policy"] = "minimal"
    except Exception:
        pass

    # Stable naming for outputs when upload path is encrypted (.enc) or anonymized.
    if upload_stem:
        rt["source_stem"] = str(upload_stem)

    # Store requested + effective settings summary (best-effort; no secrets).
    try:
        from anime_v2.config import get_settings as _gs
        from anime_v2.modes import resolve_effective_settings

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

    # External transcript/subtitle imports (optional):
    # Store under INPUT_DIR/imports/<job_id>/... and record paths in runtime.
    imports: dict[str, str] = {}
    imp_dir = (_input_imports_dir() / jid).resolve()
    imp_dir.mkdir(parents=True, exist_ok=True)
    try:
        if import_src_srt_text:
            if len(import_src_srt_text.encode("utf-8", errors="ignore")) > _MAX_IMPORT_TEXT_BYTES:
                raise HTTPException(status_code=400, detail="src_srt_text too large")
            p = (imp_dir / "src.srt").resolve()
            p.write_text(import_src_srt_text, encoding="utf-8")
            imports["src_srt_path"] = str(p)
        if import_tgt_srt_text:
            if len(import_tgt_srt_text.encode("utf-8", errors="ignore")) > _MAX_IMPORT_TEXT_BYTES:
                raise HTTPException(status_code=400, detail="tgt_srt_text too large")
            p = (imp_dir / "tgt.srt").resolve()
            p.write_text(import_tgt_srt_text, encoding="utf-8")
            imports["tgt_srt_path"] = str(p)
        if import_transcript_json_text:
            if (
                len(import_transcript_json_text.encode("utf-8", errors="ignore"))
                > _MAX_IMPORT_TEXT_BYTES
            ):
                raise HTTPException(status_code=400, detail="transcript_json_text too large")
            p = (imp_dir / "transcript.json").resolve()
            p.write_text(import_transcript_json_text, encoding="utf-8")
            imports["transcript_json_path"] = str(p)
    except HTTPException:
        raise
    except Exception:
        # best-effort; do not block job creation for import IO errors
        imports = {}
    if imports:
        rt2 = dict(job.runtime or {})
        rt2.setdefault("imports", {})
        if isinstance(rt2.get("imports"), dict):
            rt2["imports"].update(imports)
        else:
            rt2["imports"] = dict(imports)
        job.runtime = rt2
    store.put(job)
    audit_event(
        "job.submit",
        request=request,
        user_id=ident.user.id,
        meta={
            "job_id": jid,
            "duration_s": float(duration_s),
            "mode": str(mode),
            "device": str(device),
            "src_lang": str(src_lang),
            "tgt_lang": str(tgt_lang),
        },
    )
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
    # Batch submit is expensive; keep it tighter.
    _enforce_rate_limit(
        request,
        key=f"jobs:batch:user:{ident.user.id}",
        limit=5,
        per_seconds=60,
    )
    _enforce_rate_limit(
        request,
        key=f"jobs:batch:ip:{_client_ip_for_limits(request)}",
        limit=10,
        per_seconds=60,
    )

    # Disk guard once per batch
    s = get_settings()
    out_root = Path(str(getattr(store, "db_path", Path(s.output_dir)))).resolve().parent
    out_root.mkdir(parents=True, exist_ok=True)
    ensure_free_space(min_gb=int(s.min_free_gb), path=out_root)

    created_ids: list[str] = []

    async def _submit_one(
        *,
        video_path: Path,
        series_title: str,
        series_slug: str,
        season_number: int,
        episode_number: int,
        mode: str,
        device: str,
        src_lang: str,
        tgt_lang: str,
        preset: dict[str, Any] | None,
        project: dict[str, Any] | None,
        pg: str = "off",
        pg_policy_path: str = "",
        qa: bool = False,
        cache_policy: str = "full",
        project_name: str = "",
        style_guide_path: str = "",
    ) -> str:
        # ffprobe validation
        try:
            duration_s = float(_validate_media_or_400(video_path, limits=limits))
        except HTTPException:
            raise
        except Exception as ex:
            raise HTTPException(
                status_code=400, detail=f"Invalid media file (ffprobe failed): {ex}"
            ) from ex

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
            series_title=str(series_title),
            series_slug=str(series_slug),
            season_number=int(season_number),
            episode_number=int(episode_number),
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
        cp = str(cache_policy or "").strip().lower()
        if cp in {"full", "balanced", "minimal"}:
            rt["cache_policy"] = cp
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
        base_series_title, base_series_slug, base_season, base_episode = _parse_library_metadata_or_422(
            body
        )
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
            cache_policy = str(it.get("cache_policy") or "full")
            project_name = str(it.get("project") or it.get("project_name") or "")
            style_guide_path = str(it.get("style_guide_path") or "")
            # project output folder stored in runtime; validated here
            if project and project.get("output_subdir"):
                project["output_subdir"] = _sanitize_output_subdir(
                    str(project.get("output_subdir") or "")
                )
            # Batch default: episode auto-increments by item order unless explicitly set.
            idx = int(len(created_ids))
            meta_payload = {
                "series_title": base_series_title,
                "series_slug": base_series_slug,
                "season_number": base_season,
                "episode_number": base_episode + idx,
            }
            # Allow per-item override (still validated).
            for k in [
                "series_title",
                "series_slug",
                "season_number",
                "season_text",
                "episode_number",
                "episode_text",
            ]:
                if k in it:
                    meta_payload[k] = it.get(k)
            series_title, series_slug, season_number, episode_number = _parse_library_metadata_or_422(
                meta_payload
            )
            created_ids.append(
                await _submit_one(
                    video_path=video_path,
                    series_title=series_title,
                    series_slug=series_slug,
                    season_number=season_number,
                    episode_number=episode_number,
                    mode=mode,
                    device=device,
                    src_lang=src_lang,
                    tgt_lang=tgt_lang,
                    preset=preset,
                    project=project,
                    pg=pg,
                    pg_policy_path=pg_policy_path,
                    qa=qa,
                    cache_policy=cache_policy,
                    project_name=project_name,
                    style_guide_path=style_guide_path,
                )
            )
    else:
        form = await request.form()
        base_series_title, base_series_slug, base_season, base_episode = _parse_library_metadata_or_422(
            dict(form)
        )
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
        cache_policy = str(form.get("cache_policy") or "full")

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
            name = getattr(upload, "filename", "") or ""
            ext = (("." + name.rsplit(".", 1)[-1]) if "." in name else ".mp4").lower()[:8]
            if ext not in _ALLOWED_UPLOAD_EXTS:
                raise HTTPException(status_code=400, detail=f"Unsupported file extension: {ext}")
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
            idx = int(len(created_ids))
            created_ids.append(
                await _submit_one(
                    video_path=dest,
                    series_title=base_series_title,
                    series_slug=base_series_slug,
                    season_number=base_season,
                    episode_number=(base_episode + idx),
                    mode=mode,
                    device=device,
                    src_lang=src_lang,
                    tgt_lang=tgt_lang,
                    preset=preset,
                    project=project,
                    pg=pg,
                    pg_policy_path=pg_policy_path,
                    qa=qa,
                    cache_policy=cache_policy,
                )
            )

    return {"ids": created_ids}


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
    _: Identity = Depends(require_scope("read:job")),
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


@router.put("/api/jobs/{id}/tags")
async def set_job_tags(
    request: Request, id: str, ident: Identity = Depends(require_role(Role.operator))
) -> dict[str, Any]:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
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
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    rt = dict(job.runtime or {})
    rt["archived"] = True
    rt["archived_at"] = now_utc()
    store.update(id, runtime=rt)
    audit_event("job.archive", request=request, user_id=ident.user.id, meta={"job_id": id})
    return {"ok": True}


@router.post("/api/jobs/{id}/unarchive")
async def unarchive_job(
    request: Request, id: str, ident: Identity = Depends(require_role(Role.operator))
) -> dict[str, Any]:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    rt = dict(job.runtime or {})
    rt["archived"] = False
    rt["archived_at"] = None
    store.update(id, runtime=rt)
    audit_event("job.unarchive", request=request, user_id=ident.user.id, meta={"job_id": id})
    return {"ok": True}


@router.delete("/api/jobs/{id}")
async def delete_job_admin(
    request: Request, id: str, ident: Identity = Depends(require_role(Role.admin))
) -> dict[str, Any]:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    # Only allow deleting inside OUTPUT_ROOT.
    out_root = _output_root()
    base_dir = _job_base_dir(job)
    jobs_ptr = (out_root / "jobs" / id).resolve()
    for p in [base_dir, jobs_ptr]:
        try:
            p.resolve().relative_to(out_root)
        except Exception:
            raise HTTPException(
                status_code=400, detail="Refusing to delete outside output dir"
            ) from None
    # Best-effort cancel first
    try:
        q = _get_queue(request)
        await q.kill(id, reason="Deleted by admin")
    except Exception:
        pass
    with suppress(Exception):
        import shutil

        shutil.rmtree(base_dir, ignore_errors=True)
        shutil.rmtree(jobs_ptr, ignore_errors=True)
    store.delete_job(id)
    audit_event("job.delete", request=request, user_id=ident.user.id, meta={"job_id": id})
    return {"ok": True}


@router.get("/api/project-profiles")
async def list_project_profiles(_: Identity = Depends(require_scope("read:job"))) -> dict[str, Any]:
    """
    Filesystem-backed project profiles under <APP_ROOT>/projects/<name>/profile.yaml.
    Returned items are safe to expose (no secrets).
    """
    try:
        from anime_v2.projects.loader import list_project_profiles as _list
        from anime_v2.projects.loader import load_project_profile

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


@router.get("/api/jobs/{id}/overrides")
async def get_job_overrides(
    request: Request, id: str, _: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)
    try:
        from anime_v2.review.overrides import load_overrides

        return load_overrides(base_dir)
    except Exception:
        return {
            "version": 1,
            "music_regions_overrides": {"adds": [], "removes": [], "edits": []},
            "speaker_overrides": {},
            "smoothing_overrides": {"disable_segments": [], "disable_ranges": []},
        }


@router.get("/api/jobs/{id}/overrides/music/effective")
async def get_job_music_regions_effective(
    request: Request, id: str, _: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    """
    Mobile-friendly endpoint for music regions overrides UI.
    Returns the *effective* regions after applying overrides to base detection output.
    """
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)
    from anime_v2.review.overrides import effective_music_regions_for_job, load_overrides

    regions, out_path = effective_music_regions_for_job(base_dir)
    ov = load_overrides(base_dir)
    mro = ov.get("music_regions_overrides") if isinstance(ov, dict) else {}
    return {
        "version": 1,
        "job_id": id,
        "regions": regions,
        "effective_path": str(out_path),
        "overrides_counts": {
            "adds": len(mro.get("adds") or []) if isinstance(mro, dict) else 0,
            "removes": len(mro.get("removes") or []) if isinstance(mro, dict) else 0,
            "edits": len(mro.get("edits") or []) if isinstance(mro, dict) else 0,
        },
    }


@router.put("/api/jobs/{id}/overrides")
async def put_job_overrides(
    request: Request, id: str, _: Identity = Depends(require_scope("edit:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    try:
        from anime_v2.review.overrides import save_overrides

        save_overrides(base_dir, body)
        audit_event("overrides.save", request=request, user_id=_.user.id, meta={"job_id": id})
        return {"ok": True}
    except Exception as ex:
        raise HTTPException(status_code=400, detail=f"Failed to save overrides: {ex}") from ex


@router.post("/api/jobs/{id}/overrides/apply")
async def apply_job_overrides(
    request: Request, id: str, _: Identity = Depends(require_scope("edit:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)
    try:
        from anime_v2.review.overrides import apply_overrides

        rep = apply_overrides(base_dir)
        audit_event("overrides.apply", request=request, user_id=_.user.id, meta={"job_id": id})
        return {"ok": True, "report": rep.to_dict()}
    except Exception as ex:
        raise HTTPException(status_code=400, detail=f"Failed to apply overrides: {ex}") from ex


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


@router.post("/api/jobs/{id}/kill")
async def kill_job_admin(
    request: Request, id: str, _: Identity = Depends(require_role(Role.admin))
) -> dict[str, Any]:
    """
    Admin-only force-stop.
    This is stronger than "cancel" in that it is allowed even when the operator role is restricted.
    """
    store = _get_store(request)
    queue = _get_queue(request)
    await queue.kill(id, reason="Killed by admin")
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    audit_event("job.kill", request=request, user_id=_.user.id, meta={"job_id": id})
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


@router.get("/api/jobs/{id}/logs")
async def logs_alias(
    request: Request, id: str, n: int = 200, _: Identity = Depends(require_scope("read:job"))
) -> PlainTextResponse:
    # Alias for mobile clients: tail-only.
    return await tail_logs(request, id, n=n, _=_)


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
    request: Request, id: str, _: Identity = Depends(require_scope("edit:job"))
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
            try:
                data = json.loads(str(raw))
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
    speaker_overrides: dict[str, Any] = {}
    try:
        from anime_v2.review.overrides import load_overrides

        ov = load_overrides(base_dir)
        speaker_overrides = ov.get("speaker_overrides", {}) if isinstance(ov, dict) else {}
        if not isinstance(speaker_overrides, dict):
            speaker_overrides = {}
    except Exception:
        speaker_overrides = {}

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
        speaker_override = ""
        try:
            speaker_override = str(speaker_overrides.get(str(i + 1)) or "")
        except Exception:
            speaker_override = ""
        items.append(
            {
                "index": i + 1,
                "start": _fmt_ts_srt(float(s0.get("start", 0.0))),
                "end": _fmt_ts_srt(float(s0.get("end", 0.0))),
                "src_text": str(s0.get("text") or ""),
                "tgt_text": tgt_text,
                "approved": approved,
                "flags": [str(x) for x in flags],
                "speaker_override": speaker_override,
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
    request: Request, id: str, _: Identity = Depends(require_scope("edit:job"))
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
    audit_event(
        "transcript.update",
        request=request,
        user_id=_.user.id,
        meta={"job_id": id, "updates": int(len(applied)), "version": int(st["version"])},
    )
    return {"ok": True, "version": st["version"]}


@router.post("/api/jobs/{id}/overrides/speaker")
async def set_speaker_overrides_from_ui(
    request: Request, id: str, _: Identity = Depends(require_scope("edit:job"))
) -> dict[str, Any]:
    """
    Set per-segment speaker overrides (used by transcript editor UI).
    Body: { updates: [{ index: <int>, speaker_override: <str> }, ...] }
    """
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
        return {"ok": True}
    try:
        from anime_v2.review.overrides import load_overrides, save_overrides

        ov = load_overrides(base_dir)
        sp = ov.get("speaker_overrides", {})
        if not isinstance(sp, dict):
            sp = {}
        for u in updates:
            try:
                idx = int(u.get("index"))
            except Exception:
                continue
            if idx <= 0:
                continue
            val = str(u.get("speaker_override") or "").strip()
            if val:
                sp[str(idx)] = val
            else:
                sp.pop(str(idx), None)
        ov["speaker_overrides"] = sp
        save_overrides(base_dir, ov)
        audit_event(
            "overrides.speaker",
            request=request,
            user_id=_.user.id,
            meta={"job_id": id, "updates": int(len(updates))},
        )
        return {"ok": True}
    except Exception as ex:
        raise HTTPException(
            status_code=400, detail=f"Failed to update speaker overrides: {ex}"
        ) from ex


@router.post("/api/jobs/{id}/transcript/synthesize")
async def synthesize_from_approved(
    request: Request, id: str, _: Identity = Depends(require_scope("edit:job"))
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


def _rewrite_helper_formal(text: str) -> str:
    """
    Deterministic "more formal" rewrite (best-effort, English-focused).
    """
    t = " ".join(str(text or "").split()).strip()
    if not t:
        return ""
    # Expand common contractions
    repls = [
        (r"(?i)\bcan't\b", "cannot"),
        (r"(?i)\bwon't\b", "will not"),
        (r"(?i)\bdon't\b", "do not"),
        (r"(?i)\bdoesn't\b", "does not"),
        (r"(?i)\bdidn't\b", "did not"),
        (r"(?i)\bisn't\b", "is not"),
        (r"(?i)\baren't\b", "are not"),
        (r"(?i)\bwasn't\b", "was not"),
        (r"(?i)\bweren't\b", "were not"),
        (r"(?i)\bit's\b", "it is"),
        (r"(?i)\bthat's\b", "that is"),
        (r"(?i)\bthere's\b", "there is"),
        (r"(?i)\bI'm\b", "I am"),
        (r"(?i)\bI've\b", "I have"),
        (r"(?i)\bI'll\b", "I will"),
        (r"(?i)\bwe're\b", "we are"),
        (r"(?i)\bthey're\b", "they are"),
        (r"(?i)\byou're\b", "you are"),
    ]
    for pat, rep in repls:
        t = re.sub(pat, rep, t)
    return t.strip()


def _rewrite_helper_reduce_slang(text: str) -> str:
    """
    Deterministic slang reduction (best-effort, English-focused).
    """
    t = " ".join(str(text or "").split()).strip()
    if not t:
        return ""
    slang = [
        (r"(?i)\bgonna\b", "going to"),
        (r"(?i)\bwanna\b", "want to"),
        (r"(?i)\bgotta\b", "have to"),
        (r"(?i)\bkinda\b", "somewhat"),
        (r"(?i)\bsorta\b", "somewhat"),
        (r"(?i)\bain't\b", "is not"),
        (r"(?i)\by'all\b", "you all"),
        (r"(?i)\bya\b", "you"),
    ]
    for pat, rep in slang:
        t = re.sub(pat, rep, t)
    return t.strip()


@router.post("/api/jobs/{id}/review/segments/{segment_id}/helper")
async def post_job_review_helper(
    request: Request,
    id: str,
    segment_id: int,
    _: Identity = Depends(require_scope("edit:job")),
) -> dict[str, Any]:
    """
    Quick-edit helpers for mobile review loop.

    Body JSON:
      - kind: shorten10|formal|reduce_slang|apply_pg
      - text: (optional) current text
    """
    store = _get_store(request)
    _enforce_rate_limit(
        request,
        key=f"review:helper:user:{_.user.id}",
        limit=120,
        per_seconds=60,
    )
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    kind = str(body.get("kind") or "").strip().lower()
    if kind not in {"shorten10", "formal", "reduce_slang", "apply_pg"}:
        raise HTTPException(status_code=400, detail="Invalid kind")

    text = str(body.get("text") or "").strip()
    if not text:
        # fall back to current chosen_text
        with suppress(Exception):
            from anime_v2.review.state import find_segment, load_state

            st = load_state(base_dir)
            seg = find_segment(st, int(segment_id))
            if isinstance(seg, dict):
                text = str(seg.get("chosen_text") or "").strip()

    if not text:
        return {"ok": True, "kind": kind, "text": ""}

    s = get_settings()
    out = text
    provider_used = "heuristic"

    if kind == "apply_pg":
        from anime_v2.text.pg_filter import apply_pg_filter, built_in_policy

        rt = job.runtime if isinstance(job.runtime, dict) else {}
        pg = str((rt or {}).get("pg") or "pg").strip().lower()
        policy = built_in_policy("pg" if pg in {"pg", "pg13"} else "pg")
        out, _triggers = apply_pg_filter(text, policy)
    else:
        # deterministic pre-pass for style helpers
        if kind == "formal":
            out = _rewrite_helper_formal(text)
        elif kind == "reduce_slang":
            out = _rewrite_helper_reduce_slang(text)

        # "shorten10" and the optional offline LLM use the existing rewrite provider machinery.
        from anime_v2.timing.fit_text import estimate_speaking_seconds
        from anime_v2.timing.rewrite_provider import fit_with_rewrite_provider

        est = max(0.1, float(estimate_speaking_seconds(out, wps=float(s.timing_wps))))
        target_s = est * (0.90 if kind == "shorten10" else 1.0)
        fitted, _stats, attempt = fit_with_rewrite_provider(
            provider_name=str(s.rewrite_provider),
            endpoint=str(s.rewrite_endpoint) if getattr(s, "rewrite_endpoint", None) else None,
            model_path=(s.rewrite_model if getattr(s, "rewrite_model", None) else None),
            strict=bool(getattr(s, "rewrite_strict", True)),
            text=out,
            target_seconds=float(target_s),
            tolerance=float(getattr(s, "timing_tolerance", 0.10)),
            wps=float(getattr(s, "timing_wps", 2.7)),
            constraints={},
            context={"context_hint": f"helper={kind}"},
        )
        out = str(fitted or "").strip()
        provider_used = str(attempt.provider_used)

    with suppress(Exception):
        audit_event(
            "review.helper",
            request=request,
            user_id=_.user.id,
            meta={
                "job_id": id,
                "segment_id": int(segment_id),
                "kind": kind,
                "provider": provider_used,
            },
        )
    return {"ok": True, "kind": kind, "provider_used": provider_used, "text": out}


@router.post("/api/jobs/{id}/review/segments/{segment_id}/edit")
async def post_job_review_edit(
    request: Request,
    id: str,
    segment_id: int,
    _: Identity = Depends(require_scope("edit:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    _enforce_rate_limit(
        request,
        key=f"review:edit:user:{_.user.id}",
        limit=120,
        per_seconds=60,
    )
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)
    body = await request.json()
    text = str(body.get("text") or "")
    from anime_v2.review.ops import edit_segment

    try:
        edit_segment(base_dir, int(segment_id), text=text)
        audit_event(
            "review.edit",
            request=request,
            user_id=_.user.id,
            meta={"job_id": id, "segment_id": int(segment_id)},
        )
        return {"ok": True}
    except Exception as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex


@router.post("/api/jobs/{id}/review/segments/{segment_id}/regen")
async def post_job_review_regen(
    request: Request,
    id: str,
    segment_id: int,
    _: Identity = Depends(require_scope("edit:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    _enforce_rate_limit(
        request,
        key=f"review:regen:user:{_.user.id}",
        limit=60,
        per_seconds=60,
    )
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)
    from anime_v2.review.ops import regen_segment

    try:
        p = regen_segment(base_dir, int(segment_id))
        audit_event(
            "review.regen",
            request=request,
            user_id=_.user.id,
            meta={"job_id": id, "segment_id": int(segment_id)},
        )
        return {"ok": True, "audio_path": str(p)}
    except Exception as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex


@router.post("/api/jobs/{id}/review/segments/{segment_id}/lock")
async def post_job_review_lock(
    request: Request,
    id: str,
    segment_id: int,
    _: Identity = Depends(require_scope("edit:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    _enforce_rate_limit(
        request,
        key=f"review:lock:user:{_.user.id}",
        limit=120,
        per_seconds=60,
    )
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)
    from anime_v2.review.ops import lock_segment

    try:
        lock_segment(base_dir, int(segment_id))
        audit_event(
            "review.lock",
            request=request,
            user_id=_.user.id,
            meta={"job_id": id, "segment_id": int(segment_id)},
        )
        return {"ok": True}
    except Exception as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex


@router.post("/api/jobs/{id}/review/segments/{segment_id}/unlock")
async def post_job_review_unlock(
    request: Request,
    id: str,
    segment_id: int,
    _: Identity = Depends(require_scope("edit:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    _enforce_rate_limit(
        request,
        key=f"review:unlock:user:{_.user.id}",
        limit=120,
        per_seconds=60,
    )
    job = store.get(id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    base_dir = _job_base_dir(job)
    from anime_v2.review.ops import unlock_segment

    try:
        unlock_segment(base_dir, int(segment_id))
        audit_event(
            "review.unlock",
            request=request,
            user_id=_.user.id,
            meta={"job_id": id, "segment_id": int(segment_id)},
        )
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

    # candidates (master)
    mp4 = None
    mkv = None
    hls = None
    lipsync = None
    # mobile
    mobile_mp4 = None
    mobile_orig_mp4 = None
    mobile_hls = None
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

    # Mobile artifacts (preferred for iOS/Android playback)
    try:
        mdir = base_dir / "mobile"
        cand = mdir / "mobile.mp4"
        if cand.exists():
            mobile_mp4 = cand
        cand2 = mdir / "original.mp4"
        if cand2.exists():
            mobile_orig_mp4 = cand2
        # Prefer index.m3u8 when present, else master.m3u8
        cand3 = mdir / "hls" / "index.m3u8"
        if cand3.exists():
            mobile_hls = cand3
        else:
            cand4 = mdir / "hls" / "master.m3u8"
            if cand4.exists():
                mobile_hls = cand4
    except Exception:
        mobile_mp4 = None
        mobile_orig_mp4 = None
        mobile_hls = None

    out_root = _output_root()

    def rel_url(p: Path) -> str:
        rel = str(p.resolve().relative_to(out_root)).replace("\\", "/")
        return f"/files/{rel}"

    files: list[dict[str, Any]] = []
    for kind, p in [
        ("mobile_hls_manifest", mobile_hls),
        ("mobile_mp4", mobile_mp4),
        ("mobile_original_mp4", mobile_orig_mp4),
        ("hls_manifest", hls),
        ("lipsync_mp4", lipsync),
        ("mp4", mp4),
        ("mkv", mkv),
    ]:
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

    # Retention report (best-effort)
    try:
        p = base_dir / "analysis" / "retention_report.json"
        if p.exists():
            st = p.stat()
            files.append(
                {
                    "kind": "retention_report",
                    "name": p.name,
                    "path": str(p),
                    "url": rel_url(p),
                    "size_bytes": int(st.st_size),
                    "mtime": float(st.st_mtime),
                }
            )
    except Exception:
        pass

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

    # Subtitle variants under Output/<job>/subs/ (best-effort)
    try:
        subs_dir = base_dir / "subs"
        if subs_dir.exists():
            for p in sorted(subs_dir.glob("*.srt")) + sorted(subs_dir.glob("*.vtt")):
                if not p.exists():
                    continue
                try:
                    st = p.stat()
                    files.append(
                        {
                            "kind": "subs",
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
        "mobile_mp4": None,
        "mobile_original_mp4": None,
        "mobile_hls_manifest": None,
        "qa_summary": None,
        "qa_top_issues": None,
    }
    # Prefer mobile playback sources for the built-in player keys.
    if mobile_hls is not None:
        data["hls_manifest"] = {"url": rel_url(mobile_hls), "path": str(mobile_hls)}
        data["mobile_hls_manifest"] = {"url": rel_url(mobile_hls), "path": str(mobile_hls)}
    elif hls is not None:
        data["hls_manifest"] = {"url": rel_url(hls), "path": str(hls)}
    if mobile_mp4 is not None:
        data["mp4"] = {"url": rel_url(mobile_mp4), "path": str(mobile_mp4)}
        data["mobile_mp4"] = {"url": rel_url(mobile_mp4), "path": str(mobile_mp4)}
    elif mp4 is not None:
        data["mp4"] = {"url": rel_url(mp4), "path": str(mp4)}
    if lipsync is not None:
        data["lipsync_mp4"] = {"url": rel_url(lipsync), "path": str(lipsync)}
    if mkv is not None:
        data["mkv"] = {"url": rel_url(mkv), "path": str(mkv)}
    if mobile_orig_mp4 is not None:
        data["mobile_original_mp4"] = {
            "url": rel_url(mobile_orig_mp4),
            "path": str(mobile_orig_mp4),
        }

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


@router.get("/api/jobs/{id}/outputs")
async def job_outputs_alias(
    request: Request, id: str, _: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    # Alias endpoint name expected by some mobile clients.
    return await job_files(request, id, _=_)


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
    s = get_settings()
    allow_legacy = bool(getattr(s, "allow_legacy_token_login", False))

    # Authenticate:
    # - Prefer headers/cookies (mobile-safe)
    # - Allow legacy ?token= only when explicitly enabled AND peer is private (unsafe on public networks)
    auth_store: AuthStore | None = getattr(websocket.app.state, "auth_store", None)
    if auth_store is None:
        await websocket.close(code=1011)
        return
    ok = False
    try:
        token = ""
        # 1) Authorization header bearer
        auth = websocket.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
        # 2) X-Api-Key header (optional)
        if not token and bool(getattr(s, "enable_api_keys", True)):
            token = (websocket.headers.get("x-api-key") or "").strip()
        # 3) Signed session cookie (web UI mode)
        if not token:
            cookie = websocket.headers.get("cookie") or ""
            sess = ""
            for part in cookie.split(";"):
                if "=" not in part:
                    continue
                k, v = part.split("=", 1)
                if k.strip() == "session":
                    sess = v.strip()
                    break
            if sess:
                try:
                    from itsdangerous import BadSignature, URLSafeTimedSerializer  # type: ignore

                    ser = URLSafeTimedSerializer(
                        s.session_secret.get_secret_value(), salt="session"
                    )
                    token = str(ser.loads(sess, max_age=60 * 60 * 24 * 7))
                except BadSignature:
                    token = ""
        # 4) Legacy query token (unsafe) - gated
        if not token and allow_legacy:
            try:
                import ipaddress

                peer = websocket.client.host if websocket.client else ""
                ip = ipaddress.ip_address(peer) if peer else None
                if ip and (ip.is_private or ip.is_loopback):
                    token = websocket.query_params.get("token") or ""
            except Exception:
                token = ""

        if not token:
            ok = False
        elif token.startswith("dp_") and bool(getattr(s, "enable_api_keys", True)):
            parts = token.split("_", 2)
            if len(parts) == 3:
                _, prefix, _ = parts
                for k in auth_store.find_api_keys_by_prefix(prefix):
                    if verify_secret(k.key_hash, token):
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
