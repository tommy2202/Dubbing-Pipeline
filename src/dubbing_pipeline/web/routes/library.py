from __future__ import annotations

import json
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request, Response

from dubbing_pipeline.api.access import require_job_access, require_library_access
from dubbing_pipeline.api.deps import Identity, require_scope
from dubbing_pipeline.api.middleware import audit_event
from dubbing_pipeline.api.models import Role
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import now_utc
from dubbing_pipeline.security.policy_deps import secure_router
from dubbing_pipeline.web.routes.jobs_common import (
    _file_range_response,
    _get_store,
    _job_base_dir,
    _output_root,
    _privacy_blocks_voice_refs,
    _slugify_character,
    _voice_ref_audio_path_from_manifest,
    _voice_refs_manifest_path,
)

router = secure_router()


def _require_voice_profile_access(profile: dict[str, Any], ident: Identity) -> None:
    if ident.user and ident.user.role and ident.user.role.value == Role.admin.value:
        return
    created_by = str(profile.get("created_by") or "").strip()
    if created_by and str(ident.user.id) == created_by:
        return
    raise HTTPException(status_code=403, detail="Forbidden")


def _normalize_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(int(value))
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _apply_voice_profile_policy(
    *,
    existing: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    source_type = str(payload.get("source_type") or "").strip()
    if source_type not in {"user_upload", "licensed_pack", "extracted_from_media", "unknown"}:
        raise HTTPException(status_code=422, detail="source_type is required")
    scope = str(payload.get("scope") or existing.get("scope") or "private").strip().lower()
    if scope not in {"private", "friends", "global"}:
        raise HTTPException(status_code=422, detail="Invalid scope")
    share_allowed = _normalize_bool(payload.get("share_allowed"), default=bool(existing.get("share_allowed")))
    export_allowed = _normalize_bool(
        payload.get("export_allowed"), default=bool(existing.get("export_allowed"))
    )
    reuse_allowed = _normalize_bool(
        payload.get("reuse_allowed"), default=bool(existing.get("reuse_allowed"))
    )
    series_lock = str(payload.get("series_lock") or existing.get("series_lock") or "").strip()

    existing_source = str(existing.get("source_type") or "").strip()
    if existing_source == "extracted_from_media" and source_type != "extracted_from_media":
        raise HTTPException(status_code=422, detail="Cannot change source for extracted media")

    if source_type == "extracted_from_media":
        if not series_lock:
            raise HTTPException(status_code=422, detail="Series lock required for extracted media")
        if str(payload.get("series_lock") or "").strip() and series_lock != str(existing.get("series_lock") or ""):
            raise HTTPException(status_code=422, detail="Series lock is enforced for extracted media")
        if scope != "private" or share_allowed or export_allowed or reuse_allowed:
            raise HTTPException(
                status_code=422,
                detail="Extracted media voices must remain private and cannot be shared/exported",
            )
        scope = "private"
        share_allowed = False
        export_allowed = False
        reuse_allowed = False

    if source_type == "unknown":
        if scope != "private" or share_allowed or export_allowed or reuse_allowed:
            raise HTTPException(
                status_code=422,
                detail="Select a source before enabling sharing or reuse",
            )
        scope = "private"
        share_allowed = False
        export_allowed = False
        reuse_allowed = False

    if source_type in {"user_upload", "licensed_pack"}:
        if scope in {"global", "friends"} and not reuse_allowed:
            raise HTTPException(
                status_code=422,
                detail="Enable reuse before sharing globally",
            )
        if scope in {"global", "friends"} and not share_allowed:
            raise HTTPException(
                status_code=422,
                detail="Sharing must be enabled for global visibility",
            )

    return {
        "source_type": source_type,
        "scope": scope,
        "share_allowed": share_allowed,
        "export_allowed": export_allowed,
        "reuse_allowed": reuse_allowed,
        "series_lock": series_lock,
    }


@router.get("/api/series/{series_slug}/characters")
async def list_series_characters(
    request: Request,
    series_slug: str,
    ident: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    slug = str(series_slug or "").strip()
    if not slug:
        raise HTTPException(status_code=400, detail="series_slug required")
    require_library_access(store=store, ident=ident, series_slug=slug)

    # Merge disk index + DB metadata.
    items: list[dict[str, Any]] = []
    db_by_slug: dict[str, dict[str, Any]] = {}
    with suppress(Exception):
        for rec in store.list_characters_for_series(slug):
            cslug = str(rec.get("character_slug") or "").strip()
            if cslug:
                db_by_slug[cslug] = dict(rec)

    from dubbing_pipeline.voice_store.store import get_character_ref as _get_ref
    from dubbing_pipeline.voice_store.store import list_characters as _list_disk

    for it in _list_disk(slug):
        cslug = str(it.get("character_slug") or "").strip()
        if not cslug:
            continue
        db = db_by_slug.get(cslug, {})
        display = str(db.get("display_name") or it.get("display_name") or "").strip()
        updated = str(db.get("updated_at") or it.get("updated_at") or "").strip()
        created_by = str(db.get("created_by") or it.get("created_by") or "").strip()
        ref = _get_ref(slug, cslug)
        items.append(
            {
                "series_slug": slug,
                "character_slug": cslug,
                "display_name": display,
                "updated_at": updated,
                "created_by": created_by,
                "has_ref": bool(ref is not None and ref.exists()),
                "ref_path": (str(ref) if ref is not None else None),
                "audio_url": (
                    f"/api/series/{slug}/characters/{cslug}/audio" if ref is not None else None
                ),
                "download_url": (
                    f"/api/series/{slug}/characters/{cslug}/audio?download=1"
                    if ref is not None
                    else None
                ),
            }
        )

    # Include DB-only characters that exist without a saved ref yet.
    for cslug, db in db_by_slug.items():
        if any(x.get("character_slug") == cslug for x in items):
            continue
        ref = _get_ref(slug, cslug)
        items.append(
            {
                "series_slug": slug,
                "character_slug": cslug,
                "display_name": str(db.get("display_name") or "").strip(),
                "updated_at": str(db.get("updated_at") or "").strip(),
                "created_by": str(db.get("created_by") or "").strip(),
                "has_ref": bool(ref is not None and ref.exists()),
                "ref_path": (str(ref) if ref is not None else None),
                "audio_url": (
                    f"/api/series/{slug}/characters/{cslug}/audio" if ref is not None else None
                ),
                "download_url": (
                    f"/api/series/{slug}/characters/{cslug}/audio?download=1"
                    if ref is not None
                    else None
                ),
            }
        )

    items.sort(key=lambda x: str(x.get("character_slug") or ""))
    return {"ok": True, "series_slug": slug, "can_edit": True, "items": items}


@router.get("/api/series/{series_slug}/characters/{character_slug}/audio")
async def get_series_character_audio(
    request: Request,
    series_slug: str,
    character_slug: str,
    download: int | None = None,
    ident: Identity = Depends(require_scope("read:job")),
) -> Response:
    store = _get_store(request)
    slug = str(series_slug or "").strip()
    if not slug or not str(character_slug or "").strip():
        raise HTTPException(status_code=400, detail="Invalid path")
    require_library_access(store=store, ident=ident, series_slug=slug)
    from dubbing_pipeline.voice_store.store import get_character_ref

    p = get_character_ref(slug, character_slug)
    if p is None or not p.exists():
        raise HTTPException(status_code=404, detail="ref not found")
    resp = _file_range_response(
        request,
        p,
        media_type="audio/wav",
        allowed_roots=[Path(get_settings().voice_store_dir).resolve()],
    )
    if download:
        name = f"{Path(str(character_slug)).name.strip() or 'character'}.wav"
        resp.headers["content-disposition"] = f'attachment; filename="{name}"'
    return resp


@router.post("/api/series/{series_slug}/characters")
async def create_series_character(
    request: Request,
    series_slug: str,
    ident: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    slug = str(series_slug or "").strip()
    if not slug:
        raise HTTPException(status_code=400, detail="series_slug required")
    require_library_access(store=store, ident=ident, series_slug=slug)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    display_name = str(body.get("display_name") or "").strip()
    if not display_name:
        raise HTTPException(status_code=422, detail="display_name is required")
    cslug = _slugify_character(body.get("character_slug") or display_name)
    if not cslug:
        raise HTTPException(status_code=422, detail="Invalid display_name (cannot derive slug)")

    # Ensure folder + index entry exist even without a ref yet.
    from dubbing_pipeline.voice_store.store import get_series_root
    from dubbing_pipeline.utils.io import atomic_write_text, read_json

    sr = get_series_root(slug)
    (sr / "characters" / cslug / "refs").mkdir(parents=True, exist_ok=True)
    meta_path = (sr / "characters" / cslug / "meta.json").resolve()
    if not meta_path.exists():
        atomic_write_text(
            meta_path,
            json.dumps(
                {
                    "series_slug": slug,
                    "character_slug": cslug,
                    "display_name": display_name,
                    "created_by": str(ident.user.id),
                    "last_updated_ts": int(time.time()),
                    "notes": "",
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    idx_path = (sr / "index.json").resolve()
    idx = read_json(idx_path, default={})
    if not isinstance(idx, dict):
        idx = {}
    idx.setdefault("version", 1)
    idx["series_slug"] = slug
    chars = idx.get("characters")
    if not isinstance(chars, list):
        chars = []
        idx["characters"] = chars
    if not any(isinstance(x, dict) and str(x.get("character_slug") or "") == cslug for x in chars):
        chars.append(
            {
                "character_slug": cslug,
                "display_name": display_name,
                "ref_path": "",
                "updated_at": now_utc(),
                "created_by": str(ident.user.id),
            }
        )
        chars.sort(key=lambda x: str(x.get("character_slug") or "") if isinstance(x, dict) else "")
        atomic_write_text(idx_path, json.dumps(idx, indent=2, sort_keys=True), encoding="utf-8")

    rec = store.upsert_character(
        series_slug=slug,
        character_slug=cslug,
        display_name=display_name,
        ref_path="",
        created_by=str(ident.user.id),
    )
    audit_event(
        "voice.character.create",
        request=request,
        user_id=ident.user.id,
        meta={"series_slug": slug, "character_slug": cslug},
    )
    return {"ok": True, "character": rec}


@router.post("/api/series/{series_slug}/characters/{character_slug}/ref")
async def upload_series_character_ref(
    request: Request,
    series_slug: str,
    character_slug: str,
    ident: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    """
    Upload/override a series character ref.wav directly (optional UI flow).
    Body is raw wav bytes.
    """
    store = _get_store(request)
    slug = str(series_slug or "").strip()
    cslug = _slugify_character(character_slug)
    if not slug or not cslug:
        raise HTTPException(status_code=400, detail="Invalid path")
    require_library_access(store=store, ident=ident, series_slug=slug)
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty body")
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (>20MB)")
    # Save via voice_store helper
    from dubbing_pipeline.voice_store.store import save_character_ref

    tmp = (_output_root() / "_tmp" / f"{slug}_{cslug}_{int(time.time())}.wav").resolve()
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(raw)
    try:
        outp = save_character_ref(
            slug,
            cslug,
            tmp,
            job_id="upload",
            metadata={"display_name": "", "created_by": str(ident.user.id), "source": "upload"},
        )
        store.upsert_character(
            series_slug=slug,
            character_slug=cslug,
            display_name="",
            ref_path=str(outp),
            created_by=str(ident.user.id),
        )
    finally:
        with suppress(Exception):
            tmp.unlink(missing_ok=True)
    audit_event(
        "voice.character.upload_ref",
        request=request,
        user_id=ident.user.id,
        meta={"series_slug": slug, "character_slug": cslug},
    )
    return {"ok": True, "series_slug": slug, "character_slug": cslug, "ref_path": str(outp)}


@router.post("/api/series/{series_slug}/characters/{character_slug}/promote-ref")
async def promote_series_character_ref_from_job(
    request: Request,
    series_slug: str,
    character_slug: str,
    ident: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    slug = str(series_slug or "").strip()
    cslug = _slugify_character(character_slug)
    if not slug or not cslug:
        raise HTTPException(status_code=400, detail="Invalid path")
    require_library_access(store=store, ident=ident, series_slug=slug)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    job_id = str(body.get("job_id") or "").strip()
    speaker_id = Path(str(body.get("speaker_id") or "")).name.strip()
    if not job_id or not speaker_id:
        raise HTTPException(status_code=422, detail="job_id and speaker_id required")
    job = require_job_access(store=store, ident=ident, job_id=job_id)
    if str(getattr(job, "series_slug", "") or "").strip() != slug:
        raise HTTPException(status_code=400, detail="Job series_slug does not match")
    if _privacy_blocks_voice_refs(job):
        raise HTTPException(status_code=403, detail="Voice refs not available in privacy/minimal mode")

    base_dir = _job_base_dir(job)
    man_path = _voice_refs_manifest_path(base_dir)
    if not man_path.exists():
        raise HTTPException(status_code=404, detail="voice refs not found for job")
    from dubbing_pipeline.utils.io import read_json

    man = read_json(man_path, default={})
    if not isinstance(man, dict):
        raise HTTPException(status_code=500, detail="invalid voice refs manifest")
    ref_path = _voice_ref_audio_path_from_manifest(
        base_dir=base_dir, speaker_id=speaker_id, manifest=man
    )
    if ref_path is None or not ref_path.exists():
        raise HTTPException(status_code=404, detail="speaker ref not found")

    from dubbing_pipeline.voice_store.store import save_character_ref

    outp = save_character_ref(
        slug,
        cslug,
        ref_path,
        job_id=job_id,
        metadata={"display_name": "", "created_by": str(ident.user.id), "source": "promote_ref"},
    )
    store.upsert_character(
        series_slug=slug,
        character_slug=cslug,
        display_name="",
        ref_path=str(outp),
        created_by=str(ident.user.id),
    )
    audit_event(
        "voice.character.promote_ref",
        request=request,
        user_id=ident.user.id,
        meta={
            "series_slug": slug,
            "character_slug": cslug,
            "job_id": job_id,
            "speaker_id": speaker_id,
        },
    )
    return {"ok": True, "ref_path": str(outp)}


@router.delete("/api/series/{series_slug}/characters/{character_slug}")
async def delete_series_character(
    request: Request,
    series_slug: str,
    character_slug: str,
    ident: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    slug = str(series_slug or "").strip()
    cslug = _slugify_character(character_slug)
    if not slug or not cslug:
        raise HTTPException(status_code=400, detail="Invalid path")
    require_library_access(store=store, ident=ident, series_slug=slug)
    from dubbing_pipeline.voice_store.store import delete_character as del_disk

    deleted_disk = bool(del_disk(slug, cslug))
    deleted_db = bool(store.delete_character(series_slug=slug, character_slug=cslug))
    audit_event(
        "voice.character.delete",
        request=request,
        user_id=ident.user.id,
        meta={
            "series_slug": slug,
            "character_slug": cslug,
            "deleted_disk": deleted_disk,
            "deleted_db": deleted_db,
        },
    )
    return {"ok": True, "deleted": bool(deleted_disk or deleted_db)}


@router.get("/api/voices/{series_slug}/{voice_id}/versions")
async def get_voice_versions(
    request: Request,
    series_slug: str,
    voice_id: str,
    ident: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    slug = str(series_slug or "").strip()
    cslug = _slugify_character(voice_id)
    if not slug or not cslug:
        raise HTTPException(status_code=400, detail="Invalid path")
    require_library_access(store=store, ident=ident, series_slug=slug)
    from dubbing_pipeline.voice_store.store import get_versions_state

    data = get_versions_state(slug, cslug)
    return {
        "ok": True,
        "series_slug": slug,
        "voice_id": cslug,
        "current_version": int(data.get("current_version") or 0),
        "items": data.get("items") if isinstance(data.get("items"), list) else [],
    }


@router.post("/api/voices/{series_slug}/{voice_id}/rollback")
async def rollback_voice_version(
    request: Request,
    series_slug: str,
    voice_id: str,
    version: int,
    ident: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    slug = str(series_slug or "").strip()
    cslug = _slugify_character(voice_id)
    if not slug or not cslug:
        raise HTTPException(status_code=400, detail="Invalid path")
    if int(version) <= 0:
        raise HTTPException(status_code=422, detail="version is required")
    require_library_access(store=store, ident=ident, series_slug=slug)
    from dubbing_pipeline.voice_store.store import rollback_character_ref

    try:
        outp = rollback_character_ref(
            slug, cslug, version=int(version), created_by=str(ident.user.id)
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="version not found") from None
    audit_event(
        "voice.version.rollback",
        request=request,
        user_id=ident.user.id,
        meta={"series_slug": slug, "voice_id": cslug, "version": int(version)},
    )
    return {"ok": True, "voice_id": cslug, "path": str(outp)}


@router.get("/api/voices/{profile_id}/suggestions")
async def get_voice_profile_suggestions(
    request: Request,
    profile_id: str,
    status: str | None = "pending",
    ident: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    pid = str(profile_id or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="profile_id required")
    profile = store.get_voice_profile(pid)
    if profile is None:
        raise HTTPException(status_code=404, detail="voice profile not found")
    _require_voice_profile_access(profile, ident)
    status_eff = None if not status or status == "all" else str(status)
    items = store.list_voice_profile_suggestions(pid, status=status_eff)
    out_items: list[dict[str, Any]] = []
    for rec in items:
        sid = str(rec.get("suggested_profile_id") or "").strip()
        target = store.get_voice_profile(sid) if sid else None
        out_items.append(
            {
                "id": str(rec.get("id") or ""),
                "profile_id": str(rec.get("voice_profile_id") or ""),
                "suggested_profile_id": sid,
                "similarity": float(rec.get("similarity") or 0.0),
                "status": str(rec.get("status") or ""),
                "created_at": rec.get("created_at"),
                "updated_at": rec.get("updated_at"),
                "suggested_display_name": (
                    str(target.get("display_name") or "") if isinstance(target, dict) else ""
                ),
                "suggested_series_lock": (
                    str(target.get("series_lock") or "") if isinstance(target, dict) else ""
                ),
                "suggested_scope": (
                    str(target.get("scope") or "") if isinstance(target, dict) else ""
                ),
                "reuse_allowed": bool(target.get("reuse_allowed") or False)
                if isinstance(target, dict)
                else False,
            }
        )
    return {"ok": True, "profile_id": pid, "items": out_items}


@router.post("/api/voices/{profile_id}/accept_suggestion")
async def accept_voice_profile_suggestion(
    request: Request,
    profile_id: str,
    ident: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    pid = str(profile_id or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="profile_id required")
    profile = store.get_voice_profile(pid)
    if profile is None:
        raise HTTPException(status_code=404, detail="voice profile not found")
    _require_voice_profile_access(profile, ident)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    suggestion_id = str(body.get("suggestion_id") or "").strip()
    action = str(body.get("action") or "use_existing").strip().lower()
    if not suggestion_id:
        raise HTTPException(status_code=422, detail="suggestion_id required")
    if action not in {"use_existing", "keep_separate"}:
        raise HTTPException(status_code=422, detail="Invalid action")
    sugg = store.get_voice_profile_suggestion(suggestion_id)
    if sugg is None:
        raise HTTPException(status_code=404, detail="suggestion not found")
    if str(sugg.get("voice_profile_id") or "") != pid:
        raise HTTPException(status_code=403, detail="Forbidden")
    if str(sugg.get("status") or "") not in {"pending", "accepted"}:
        raise HTTPException(status_code=409, detail="Suggestion already resolved")

    alias = None
    if action == "use_existing":
        alias = store.upsert_voice_profile_alias(
            voice_profile_id=pid,
            alias_of_voice_profile_id=str(sugg.get("suggested_profile_id") or ""),
            confidence=float(sugg.get("similarity") or 0.0),
            approved_by_admin=False,
            approved_at=None,
        )
        store.set_voice_profile_suggestion_status(suggestion_id, status="accepted")
        audit_event(
            "voice_profile.suggestion.accept",
            request=request,
            user_id=ident.user.id,
            meta={"profile_id": pid, "suggestion_id": suggestion_id},
        )
    else:
        store.set_voice_profile_suggestion_status(suggestion_id, status="rejected")
        audit_event(
            "voice_profile.suggestion.reject",
            request=request,
            user_id=ident.user.id,
            meta={"profile_id": pid, "suggestion_id": suggestion_id},
        )
    return {
        "ok": True,
        "profile_id": pid,
        "suggestion_id": suggestion_id,
        "status": "accepted" if action == "use_existing" else "rejected",
        "alias": alias,
    }


@router.post("/api/voices/{profile_id}/consent")
async def update_voice_profile_consent(
    request: Request,
    profile_id: str,
    ident: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    pid = str(profile_id or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="profile_id required")
    prof = store.get_voice_profile(pid)
    if prof is None:
        raise HTTPException(status_code=404, detail="voice profile not found")
    _require_voice_profile_access(prof, ident)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    try:
        normalized = _apply_voice_profile_policy(existing=prof, payload=body)
    except HTTPException as ex:
        audit_event(
            "voice_profile.policy_blocked",
            request=request,
            user_id=ident.user.id,
            meta={
                "profile_id": pid,
                "reason": str(ex.detail),
                "source_type": str(body.get("source_type") or ""),
                "scope": str(body.get("scope") or ""),
            },
        )
        raise
    rec = store.upsert_voice_profile(
        profile_id=pid,
        display_name=str(prof.get("display_name") or ""),
        created_by=str(prof.get("created_by") or ""),
        scope=str(normalized["scope"]),
        series_lock=str(normalized["series_lock"] or "") or None,
        source_type=str(normalized["source_type"]),
        export_allowed=bool(normalized["export_allowed"]),
        share_allowed=bool(normalized["share_allowed"]),
        reuse_allowed=1 if bool(normalized["reuse_allowed"]) else 0,
        expires_at=prof.get("expires_at"),
        embedding_vector=prof.get("embedding_vector"),
        embedding_model_id=str(prof.get("embedding_model_id") or ""),
        metadata_json=prof.get("metadata_json"),
    )
    audit_event(
        "voice_profile.consent_updated",
        request=request,
        user_id=ident.user.id,
        meta={"profile_id": pid, "source_type": normalized["source_type"]},
    )
    return {"ok": True, "profile_id": pid, "item": rec}
