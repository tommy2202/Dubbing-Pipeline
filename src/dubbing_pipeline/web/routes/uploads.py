from __future__ import annotations

import hashlib
import re
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from dubbing_pipeline.api.access import require_upload_access
from dubbing_pipeline.api.deps import Identity, require_scope
from dubbing_pipeline.api.middleware import audit_event
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.limits import get_limits
from dubbing_pipeline.security.crypto import CryptoConfigError, encrypt_file, encryption_enabled_for
from dubbing_pipeline.web.routes.jobs_common import (
    _ALLOWED_UPLOAD_EXTS,
    _ALLOWED_UPLOAD_MIME,
    _app_root,
    _client_ip_for_limits,
    _enforce_rate_limit,
    _get_store,
    _input_dir,
    _input_uploads_dir,
    _new_short_id,
    _now_iso,
    _safe_filename,
    _sha256_hex,
    _upload_lock,
    _validate_media_or_400,
)

router = APIRouter()


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
    rec = require_upload_access(store=store, ident=ident, upload_id=upload_id)
    return {
        "upload_id": str(rec.get("id") or upload_id),
        "total_bytes": int(rec.get("total_bytes") or 0),
        "chunk_bytes": int(rec.get("chunk_bytes") or 0),
        "received_bytes": int(rec.get("received_bytes") or 0),
        "completed": bool(rec.get("completed")),
        "received": rec.get("received") if isinstance(rec.get("received"), dict) else {},
    }


@router.get("/api/uploads/{upload_id}/status")
async def uploads_status_minimal(
    request: Request, upload_id: str, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    """
    Minimal upload state for mobile resume flows.
    """
    store = _get_store(request)
    rec = require_upload_access(store=store, ident=ident, upload_id=upload_id)
    total_bytes = int(rec.get("total_bytes") or 0)
    chunk_bytes = int(rec.get("chunk_bytes") or 0)
    received = rec.get("received") if isinstance(rec.get("received"), dict) else {}
    chunks_received = int(len(received))
    total_chunks = int((total_bytes + chunk_bytes - 1) // chunk_bytes) if chunk_bytes > 0 else 0
    next_expected = 0
    if total_chunks > 0:
        for i in range(total_chunks):
            if str(i) not in received:
                next_expected = i
                break
        else:
            next_expected = total_chunks
    state = "completed" if bool(rec.get("completed")) else "in_progress"
    with suppress(Exception):
        audit_event(
            "upload.status",
            request=request,
            user_id=ident.user.id,
            meta={
                "upload_id": str(upload_id),
                "state": state,
                "bytes_received": int(rec.get("received_bytes") or 0),
            },
        )
    return {
        "upload_id": str(rec.get("id") or upload_id),
        "state": state,
        "bytes_received": int(rec.get("received_bytes") or 0),
        "chunks_received": int(chunks_received),
        "next_expected_chunk": int(next_expected),
        "total_bytes": int(total_bytes),
        "chunk_bytes": int(chunk_bytes),
        "total_chunks": int(total_chunks),
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
    rec = require_upload_access(store=store, ident=ident, upload_id=upload_id)
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
        rec2 = require_upload_access(store=store, ident=ident, upload=rec2)
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
    rec = require_upload_access(store=store, ident=ident, upload_id=upload_id)

    body = await request.json()
    if not isinstance(body, dict):
        body = {}
    final_sha = str(body.get("final_sha256") or "").strip().lower()
    if final_sha and not re.fullmatch(r"[0-9a-f]{64}", final_sha):
        raise HTTPException(status_code=400, detail="Invalid final_sha256")

    async with _upload_lock(upload_id):
        rec2 = store.get_upload(upload_id) or rec
        rec2 = require_upload_access(store=store, ident=ident, upload=rec2)
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
