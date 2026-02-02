from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from dubbing_pipeline.api.access import require_job_access
from dubbing_pipeline.api.deps import Identity
from dubbing_pipeline.api.middleware import audit_event
from dubbing_pipeline.web.routes.jobs_common import (
    _apply_transcript_updates,
    _get_store,
    _job_base_dir,
    _load_transcript_store,
    _output_root,
)

from .helpers import _fmt_ts_srt, _parse_srt


async def get_job_overrides(
    request: Request, id: str, ident: Identity
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    try:
        from dubbing_pipeline.review.overrides import load_overrides

        return load_overrides(base_dir)
    except Exception:
        return {
            "version": 1,
            "music_regions_overrides": {"adds": [], "removes": [], "edits": []},
            "speaker_overrides": {},
            "smoothing_overrides": {"disable_segments": [], "disable_ranges": []},
        }


async def get_job_music_regions_effective(
    request: Request, id: str, ident: Identity
) -> dict[str, Any]:
    """
    Mobile-friendly endpoint for music regions overrides UI.
    Returns the *effective* regions after applying overrides to base detection output.
    """
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    from dubbing_pipeline.review.overrides import effective_music_regions_for_job, load_overrides

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


async def put_job_overrides(
    request: Request, id: str, ident: Identity
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    try:
        from dubbing_pipeline.review.overrides import save_overrides

        save_overrides(base_dir, body)
        audit_event("overrides.save", request=request, user_id=ident.user.id, meta={"job_id": id})
        return {"ok": True}
    except Exception as ex:
        raise HTTPException(status_code=400, detail=f"Failed to save overrides: {ex}") from ex


async def apply_job_overrides(
    request: Request, id: str, ident: Identity
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    try:
        from dubbing_pipeline.review.overrides import apply_overrides

        rep = apply_overrides(base_dir)
        audit_event("overrides.apply", request=request, user_id=ident.user.id, meta={"job_id": id})
        return {"ok": True, "report": rep.to_dict()}
    except Exception as ex:
        raise HTTPException(status_code=400, detail=f"Failed to apply overrides: {ex}") from ex


async def get_job_characters(
    request: Request, id: str, ident: Identity
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    rt = dict(job.runtime or {})
    items = rt.get("voice_map", [])
    if not isinstance(items, list):
        items = []
    return {"items": items}


async def put_job_characters(
    request: Request, id: str, ident: Identity
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)

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
            pass

    rt = dict(job.runtime or {})
    rt["voice_map"] = items
    store.update(id, runtime=rt)
    return {"ok": True, "items": items}


async def get_job_transcript(
    request: Request,
    id: str,
    page: int,
    per_page: int,
    ident: Identity,
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
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
        from dubbing_pipeline.review.overrides import load_overrides

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


async def put_job_transcript(
    request: Request, id: str, ident: Identity
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)

    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("updates"), list):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    updates = [u for u in body.get("updates", []) if isinstance(u, dict)]
    if not updates:
        return {"ok": True, "version": int(_load_transcript_store(base_dir).get("version") or 0)}

    version, applied = _apply_transcript_updates(base_dir=base_dir, updates=updates)

    # Persist version on job runtime for visibility.
    rt = dict(job.runtime or {})
    rt["transcript_version"] = int(version)
    store.update(id, runtime=rt)
    audit_event(
        "transcript.update",
        request=request,
        user_id=ident.user.id,
        meta={"job_id": id, "updates": int(len(applied)), "version": int(version)},
    )
    return {"ok": True, "version": int(version)}


async def set_speaker_overrides_from_ui(
    request: Request, id: str, ident: Identity
) -> dict[str, Any]:
    """
    Set per-segment speaker overrides (used by transcript editor UI).
    Body: { updates: [{ index: <int>, speaker_override: <str> }, ...] }
    """
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("updates"), list):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    updates = [u for u in body.get("updates", []) if isinstance(u, dict)]
    if not updates:
        return {"ok": True}
    try:
        from dubbing_pipeline.review.overrides import load_overrides, save_overrides

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
            user_id=ident.user.id,
            meta={"job_id": id, "updates": int(len(updates))},
        )
        return {"ok": True}
    except Exception as ex:
        raise HTTPException(
            status_code=400, detail=f"Failed to update speaker overrides: {ex}"
        ) from ex
