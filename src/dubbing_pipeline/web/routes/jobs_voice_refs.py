from __future__ import annotations

import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from dubbing_pipeline.api.access import require_job_access, require_library_access
from dubbing_pipeline.api.deps import Identity, require_role, require_scope
from dubbing_pipeline.api.middleware import audit_event
from dubbing_pipeline.api.models import Role
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.security import policy
from dubbing_pipeline.web.routes.jobs_common import (
    _enforce_rate_limit,
    _file_range_response,
    _get_store,
    _job_base_dir,
    _privacy_blocks_voice_refs,
    _slugify_character,
    _voice_ref_allowed_roots,
    _voice_ref_audio_path_from_manifest,
    _voice_ref_override_path,
    _voice_refs_manifest_path,
)

router = APIRouter(
    dependencies=[
        Depends(policy.require_request_allowed),
        Depends(policy.require_invite_member),
    ]
)


def _normalize_voice_strategy(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v in {"clone", "zero-shot", "zeroshot"}:
        return "clone"
    if v in {"preset", "voice"}:
        return "preset"
    if v in {"original", "keep-original", "keep_original", "keep"}:
        return "original"
    return ""


def _voice_mapping_from_job(job) -> dict[str, dict[str, Any]]:
    try:
        rt = dict(job.runtime or {})
    except Exception:
        rt = {}
    items = rt.get("voice_map", [])
    if not isinstance(items, list):
        items = []
    out: dict[str, dict[str, Any]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = str(it.get("speaker_id") or it.get("character_id") or "").strip()
        if not sid:
            continue
        strat = _normalize_voice_strategy(it.get("speaker_strategy") or it.get("strategy") or "")
        preset = str(it.get("tts_speaker") or "").strip()
        if strat:
            out[sid] = {"strategy": strat, "preset": preset}
        elif preset:
            out[sid] = {"strategy": "preset", "preset": preset}
    return out


def _speaker_label(speaker_id: str) -> str:
    sid = str(speaker_id or "").strip()
    if not sid:
        return "Speaker"
    if sid.upper().startswith("SPEAKER_"):
        tail = sid.split("_", 1)[1]
        try:
            n = int(tail)
            return f"Speaker {n}"
        except Exception:
            return sid.replace("_", " ").title()
    return sid


@router.get("/api/jobs/{id}/voice_refs")
async def get_job_voice_refs(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    man_path = _voice_refs_manifest_path(base_dir)
    if not man_path.exists():
        return {"ok": True, "available": False, "items": {}, "note": "voice refs not built yet"}
    from dubbing_pipeline.utils.io import read_json

    data = read_json(man_path, default={})
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="Invalid voice refs manifest")

    # Best-effort: load latest TTS speaker report for cloned-vs-fallback display.
    tts_report: dict[str, Any] = {}
    with suppress(Exception):
        tts_path = (base_dir / "work" / str(id) / "tts_manifest.json").resolve()
        if tts_path.exists():
            tj = read_json(tts_path, default={})
            if isinstance(tj, dict) and isinstance(tj.get("speaker_report"), dict):
                tts_report = tj.get("speaker_report")  # type: ignore[assignment]
        # Prefer persisted analysis copy when available (work dir is cleaned).
        tts_path2 = (base_dir / "analysis" / "tts_manifest.json").resolve()
        if tts_path2.exists():
            tj = read_json(tts_path2, default={})
            if isinstance(tj, dict) and isinstance(tj.get("speaker_report"), dict):
                tts_report = tj.get("speaker_report")  # type: ignore[assignment]

    # Series-scoped character mapping (best-effort; shown in UI).
    series_slug = str(getattr(job, "series_slug", "") or "").strip()
    speaker_map: dict[str, str] = {}
    if series_slug:
        with suppress(Exception):
            for rec in store.list_speaker_mappings(str(id)):
                sid = str(rec.get("speaker_id") or "").strip()
                cslug = str(rec.get("character_slug") or "").strip()
                if sid and cslug:
                    speaker_map[sid] = cslug
    # Track-clone voice profile mapping (best-effort; speaker_id -> profile_id).
    voice_profile_map: dict[str, dict[str, Any]] = {}
    try:
        rt = dict(job.runtime or {})
        vp = rt.get("voice_profile_map")
        if isinstance(vp, dict):
            for sid, rec in vp.items():
                if not str(sid or "").strip():
                    continue
                if isinstance(rec, dict):
                    voice_profile_map[str(sid)] = dict(rec)
                else:
                    voice_profile_map[str(sid)] = {"profile_id": str(rec)}
    except Exception:
        voice_profile_map = {}

    allow_audio = not _privacy_blocks_voice_refs(job)
    items_out: dict[str, Any] = {}
    items = data.get("items") if isinstance(data.get("items"), dict) else {}
    for sid, rec in items.items():
        if not isinstance(rec, dict):
            continue
        safe_sid = Path(str(sid or "")).name.strip()
        if not safe_sid:
            continue
        ov = _voice_ref_override_path(base_dir, safe_sid)
        if ov.exists() and ov.is_file():
            eff = str(ov)
        else:
            eff = str(rec.get("job_ref_path") or rec.get("ref_path") or "")

        clone_status = "unknown"
        fallback_reason = None
        rep = tts_report.get(safe_sid) if isinstance(tts_report, dict) else None
        if isinstance(rep, dict):
            if bool(rep.get("clone_succeeded") or False):
                clone_status = "cloned"
            else:
                # If clone wasn't successful, treat as fallback (covers pass1 preset/basic and pass2 failures).
                clone_status = "fallback"
                frs = rep.get("fallback_reasons")
                if isinstance(frs, list) and frs:
                    fallback_reason = str(frs[0])
        # Character ref info (best-effort)
        character_slug = str(speaker_map.get(safe_sid) or "").strip()
        character_ref_path = None
        used_ref_kind = None
        with suppress(Exception):
            if series_slug and character_slug:
                from dubbing_pipeline.voice_store.store import get_character_ref

                cref = get_character_ref(series_slug, character_slug)
                if cref is not None:
                    character_ref_path = str(cref)
                    used_ref_kind = "character"
        with suppress(Exception):
            # If TTS report indicates a voice_store path, flag it as character ref used.
            if isinstance(rep, dict):
                ru = rep.get("refs_used")
                if isinstance(ru, list) and ru:
                    sru = " ".join([str(x) for x in ru])
                    if "/voice_store/" in sru or "/voices/" in sru:
                        used_ref_kind = "character"
                    else:
                        used_ref_kind = "speaker"
        vp = voice_profile_map.get(safe_sid) if isinstance(voice_profile_map, dict) else None
        vp_id = str((vp or {}).get("profile_id") or "").strip() if isinstance(vp, dict) else ""
        vp_meta: dict[str, Any] | None = None
        if vp_id:
            try:
                prof = store.get_voice_profile(vp_id)
                if isinstance(prof, dict):
                    vp_meta = {
                        "id": str(prof.get("id") or ""),
                        "source_type": str(prof.get("source_type") or "unknown"),
                        "scope": str(prof.get("scope") or "private"),
                        "series_lock": str(prof.get("series_lock") or ""),
                        "share_allowed": bool(prof.get("share_allowed") or False),
                        "export_allowed": bool(prof.get("export_allowed") or False),
                        "reuse_allowed": bool(prof.get("reuse_allowed") or False),
                        "display_name": str(prof.get("display_name") or ""),
                    }
            except Exception:
                vp_meta = None

        items_out[safe_sid] = {
            "speaker_id": safe_sid,
            "duration_s": float(rec.get("duration_s") or 0.0),
            "target_s": float(rec.get("target_s") or 0.0),
            "warnings": rec.get("warnings") if isinstance(rec.get("warnings"), list) else [],
            "override": bool(ov.exists() and ov.is_file()),
            "effective_ref_path": eff,
            "character_slug": character_slug or None,
            "character_ref_path": character_ref_path,
            "used_ref_kind": used_ref_kind,
            "voice_profile_id": vp_id,
            "voice_profile": vp_meta,
            "allow_audio": bool(allow_audio),
            "audio_url": (
                f"/api/jobs/{id}/voice_refs/{safe_sid}/audio" if allow_audio else None
            ),
            "download_url": (
                f"/api/jobs/{id}/voice_refs/{safe_sid}/audio?download=1" if allow_audio else None
            ),
            "clone_status": clone_status,
            "fallback_reason": fallback_reason,
        }
    return {
        "ok": True,
        "available": True,
        "allow_audio": bool(allow_audio),
        "items": items_out,
    }


@router.get("/api/jobs/{id}/speakers")
async def get_job_speakers(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    voice_map = _voice_mapping_from_job(job)
    data = await get_job_voice_refs(request, id, ident)
    items: list[dict[str, Any]] = []
    if data.get("available") and isinstance(data.get("items"), dict):
        for sid, rec in sorted((data.get("items") or {}).items()):
            if not isinstance(rec, dict):
                continue
            speaker_id = str(rec.get("speaker_id") or sid or "").strip()
            if not speaker_id:
                continue
            items.append(
                {
                    "speaker_id": speaker_id,
                    "label": _speaker_label(speaker_id),
                    "audio_url": rec.get("audio_url"),
                    "download_url": rec.get("download_url"),
                    "ref_path": rec.get("effective_ref_path"),
                    "allow_audio": bool(rec.get("allow_audio")),
                    "mapping": voice_map.get(speaker_id, {}),
                }
            )
        return {"ok": True, "available": True, "items": items}

    fallback_sid = "SPEAKER_01"
    items.append(
        {
            "speaker_id": fallback_sid,
            "label": _speaker_label(fallback_sid),
            "audio_url": None,
            "download_url": None,
            "ref_path": None,
            "allow_audio": False,
            "mapping": voice_map.get(fallback_sid, {}),
        }
    )
    return {"ok": True, "available": False, "items": items}


@router.get("/api/jobs/{id}/voice_refs/{speaker_id}/audio")
async def get_job_voice_ref_audio(
    request: Request,
    id: str,
    speaker_id: str,
    download: int | None = None,
    ident: Identity = Depends(require_scope("read:job")),
) -> Response:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    if _privacy_blocks_voice_refs(job):
        raise HTTPException(status_code=403, detail="Voice refs not available in privacy/minimal mode")
    base_dir = _job_base_dir(job)
    man_path = _voice_refs_manifest_path(base_dir)
    if not man_path.exists():
        raise HTTPException(status_code=404, detail="voice refs not found")
    from dubbing_pipeline.utils.io import read_json

    man = read_json(man_path, default={})
    if not isinstance(man, dict):
        raise HTTPException(status_code=500, detail="invalid voice refs manifest")
    p = _voice_ref_audio_path_from_manifest(base_dir=base_dir, speaker_id=speaker_id, manifest=man)
    if p is None:
        raise HTTPException(status_code=404, detail="voice ref not found")
    resp = _file_range_response(
        request, p, media_type="audio/wav", allowed_roots=_voice_ref_allowed_roots(base_dir)
    )
    if download:
        # best-effort attachment name
        name = f"{Path(str(speaker_id)).name.strip() or 'speaker'}.wav"
        resp.headers["content-disposition"] = f'attachment; filename="{name}"'
    return resp


@router.post("/api/jobs/{id}/voice_refs/{speaker_id}/override")
async def post_job_voice_ref_override(
    request: Request,
    id: str,
    speaker_id: str,
    ident: Identity = Depends(require_role(Role.admin)),
) -> dict[str, Any]:
    """
    Admin-only: upload a per-job speaker ref WAV override.
    Stored under Output/<job>/analysis/voice_refs/<speaker_id>.wav (job-local).
    """
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    if _privacy_blocks_voice_refs(job):
        raise HTTPException(status_code=403, detail="Voice refs not available in privacy/minimal mode")

    sid = Path(str(speaker_id or "")).name.strip()
    if not sid:
        raise HTTPException(status_code=400, detail="Invalid speaker_id")

    _enforce_rate_limit(
        request,
        key=f"voice_ref:override:user:{ident.user.id}",
        limit=60,
        per_seconds=60,
    )

    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty body")
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (>20MB)")

    outp = _voice_ref_override_path(base_dir, sid)
    outp.parent.mkdir(parents=True, exist_ok=True)
    tmp = outp.with_suffix(".upload.tmp")
    tmp.write_bytes(raw)

    # Normalize to 16kHz mono PCM wav (best-effort) for downstream clone engines.
    try:
        from dubbing_pipeline.utils.ffmpeg_safe import run_ffmpeg

        s = get_settings()
        norm = outp.with_suffix(".norm.wav")
        run_ffmpeg(
            [
                str(s.ffmpeg_bin),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(tmp),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(norm),
            ],
            timeout_s=120,
            retries=0,
            capture=True,
        )
        norm.replace(outp)
        tmp.unlink(missing_ok=True)
    except Exception:
        # Fall back to the raw upload if ffmpeg is unavailable.
        tmp.replace(outp)

    # Annotate manifest (best-effort): keep original ref_path but add override_path.
    with suppress(Exception):
        from dubbing_pipeline.utils.io import read_json, write_json

        man_path = _voice_refs_manifest_path(base_dir)
        if man_path.exists():
            man = read_json(man_path, default={})
            if isinstance(man, dict) and isinstance(man.get("items"), dict):
                it = man["items"].get(sid)
                if isinstance(it, dict):
                    it["override_path"] = str(outp)
                    it["override_uploaded_at"] = time.time()
                    it["override_uploaded_by"] = str(ident.user.id)
                    man["items"][sid] = it
                    write_json(man_path, man, indent=2)

    audit_event(
        "voice_ref.override",
        request=request,
        user_id=ident.user.id,
        meta={"job_id": id, "speaker_id": sid},
    )
    return {"ok": True, "speaker_id": sid, "path": str(outp)}


@router.post("/api/jobs/{job_id}/speaker-mapping")
async def post_job_speaker_mapping(
    request: Request,
    job_id: str,
    ident: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=str(job_id))
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    speaker_id = Path(str(body.get("speaker_id") or "")).name.strip()
    character_slug = _slugify_character(body.get("character_slug") or "")
    locked = bool(body.get("locked") if body.get("locked") is not None else True)
    if not speaker_id or not character_slug:
        raise HTTPException(status_code=422, detail="speaker_id and character_slug required")
    series_slug = str(getattr(job, "series_slug", "") or "").strip()
    if not series_slug:
        raise HTTPException(status_code=400, detail="job has no series_slug")
    require_library_access(store=store, ident=ident, series_slug=series_slug)
    rec = store.upsert_speaker_mapping(
        job_id=str(job_id),
        speaker_id=speaker_id,
        character_slug=character_slug,
        confidence=float(body.get("confidence") or 1.0),
        locked=bool(locked),
        created_by=str(ident.user.id),
    )
    audit_event(
        "voice.speaker_mapping.save",
        request=request,
        user_id=ident.user.id,
        meta={
            "job_id": str(job_id),
            "speaker_id": speaker_id,
            "character_slug": character_slug,
            "locked": bool(locked),
        },
    )
    return {"ok": True, "mapping": rec}


@router.post("/api/jobs/{id}/voice-mapping")
async def post_job_voice_mapping(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    body = await request.json()
    items_in: list[dict[str, Any]] = []
    if isinstance(body, dict) and isinstance(body.get("items"), list):
        items_in = [dict(x) for x in body.get("items", []) if isinstance(x, dict)]
    elif isinstance(body, list):
        items_in = [dict(x) for x in body if isinstance(x, dict)]
    else:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not items_in:
        raise HTTPException(status_code=422, detail="items required")

    try:
        rt = dict(job.runtime or {})
    except Exception:
        rt = {}
    existing = rt.get("voice_map", [])
    if not isinstance(existing, list):
        existing = []
    merged: dict[str, dict[str, Any]] = {}
    for it in existing:
        if not isinstance(it, dict):
            continue
        sid = str(it.get("speaker_id") or it.get("character_id") or "").strip()
        if not sid:
            continue
        merged[sid] = dict(it)

    for it in items_in:
        sid = Path(str(it.get("speaker_id") or it.get("character_id") or "")).name.strip()
        if not sid:
            raise HTTPException(status_code=422, detail="speaker_id required")
        strat = _normalize_voice_strategy(it.get("strategy") or it.get("speaker_strategy") or "")
        if not strat:
            raise HTTPException(status_code=422, detail="strategy required")
        rec = merged.get(sid, {})
        rec["character_id"] = sid
        rec["speaker_id"] = sid
        label = str(it.get("label") or rec.get("label") or "").strip()
        if label:
            rec["label"] = label
        rec["speaker_strategy"] = strat
        if strat == "preset":
            preset = str(it.get("preset") or it.get("tts_speaker") or "").strip() or "default"
            rec["tts_speaker"] = preset
        merged[sid] = rec

    items_out = [merged[k] for k in sorted(merged.keys())]
    rt["voice_map"] = items_out
    store.update(id, runtime=rt)
    audit_event(
        "voice.mapping.save",
        request=request,
        user_id=ident.user.id,
        meta={"job_id": id, "count": len(items_out)},
    )
    return {"ok": True, "items": items_out}


@router.get("/api/jobs/{job_id}/speaker-mapping")
async def get_job_speaker_mapping(
    request: Request,
    job_id: str,
    ident: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    _ = require_job_access(store=store, ident=ident, job_id=str(job_id))
    items = store.list_speaker_mappings(str(job_id))
    return {"ok": True, "items": items}
