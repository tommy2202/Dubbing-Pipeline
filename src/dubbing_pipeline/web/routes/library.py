from __future__ import annotations

import json
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from dubbing_pipeline.api.access import require_job_access, require_library_access
from dubbing_pipeline.api.deps import Identity, require_scope
from dubbing_pipeline.api.middleware import audit_event
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import now_utc
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

router = APIRouter()


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
