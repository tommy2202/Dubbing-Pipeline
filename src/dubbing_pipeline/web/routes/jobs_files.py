from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from dubbing_pipeline.api.access import require_job_access
from dubbing_pipeline.api.deps import Identity, require_scope
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.security import policy
from dubbing_pipeline.web.routes.jobs_common import (
    _file_range_response,
    _get_store,
    _job_base_dir,
    _output_root,
    _stream_chunk_mp4_path,
    _stream_manifest_path,
)

router = APIRouter(
    dependencies=[
        Depends(policy.require_request_allowed),
        Depends(policy.require_authenticated_user),
    ]
)


def _audio_preview_path(base_dir: Path) -> tuple[Path, str] | None:
    for name, media_type in [
        ("audio_preview.m4a", "audio/mp4"),
        ("audio_preview.mp3", "audio/mpeg"),
    ]:
        p = (base_dir / "preview" / name).resolve()
        if p.exists() and p.is_file():
            return p, media_type
    return None


def _lowres_preview_path(base_dir: Path) -> Path | None:
    p = (base_dir / "preview" / "preview_lowres.mp4").resolve()
    return p if p.exists() and p.is_file() else None


@router.get("/api/jobs/{id}/files")
async def job_files(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id, allow_shared_read=True)
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
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    # Alias endpoint name expected by some mobile clients.
    return await job_files(request, id, ident=ident)


@router.get("/api/jobs/{id}/preview/audio")
async def job_preview_audio(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
) -> Response:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id, allow_shared_read=True)
    if not bool(getattr(get_settings(), "enable_audio_preview", False)):
        raise HTTPException(status_code=404, detail="preview disabled")
    base_dir = _job_base_dir(job)
    found = _audio_preview_path(base_dir)
    if not found:
        raise HTTPException(status_code=404, detail="preview not found")
    path, media_type = found
    return _file_range_response(request, path, media_type=media_type, allowed_roots=[base_dir])


@router.get("/api/jobs/{id}/preview/lowres")
async def job_preview_lowres(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
) -> Response:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id, allow_shared_read=True)
    if not bool(getattr(get_settings(), "enable_lowres_preview", False)):
        raise HTTPException(status_code=404, detail="preview disabled")
    base_dir = _job_base_dir(job)
    p = _lowres_preview_path(base_dir)
    if p is None:
        raise HTTPException(status_code=404, detail="preview not found")
    return _file_range_response(request, p, media_type="video/mp4", allowed_roots=[base_dir])


@router.get("/api/jobs/{id}/stream/manifest")
async def job_stream_manifest(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id, allow_shared_read=True)
    base_dir = _job_base_dir(job)
    p = _stream_manifest_path(base_dir)
    if not p.exists():
        raise HTTPException(status_code=404, detail="stream manifest not found")
    from dubbing_pipeline.utils.io import read_json

    data = read_json(p, default={})
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="invalid manifest")
    return data


@router.get("/api/jobs/{id}/stream/chunks/{chunk_idx}")
async def job_stream_chunk(
    request: Request,
    id: str,
    chunk_idx: int,
    ident: Identity = Depends(require_scope("read:job")),
) -> Response:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id, allow_shared_read=True)
    base_dir = _job_base_dir(job)
    p = _stream_chunk_mp4_path(base_dir, int(chunk_idx))
    if p is None:
        raise HTTPException(status_code=404, detail="chunk not found")
    return _file_range_response(request, p, media_type="video/mp4", allowed_roots=[base_dir])


@router.get("/api/jobs/{id}/qrcode")
async def job_qrcode(request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))):
    store = _get_store(request)
    _ = require_job_access(store=store, ident=ident, job_id=id, allow_shared_read=True)
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
