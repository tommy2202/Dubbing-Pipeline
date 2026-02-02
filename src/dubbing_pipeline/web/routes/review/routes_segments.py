from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from dubbing_pipeline.api.access import require_job_access
from dubbing_pipeline.api.deps import Identity
from dubbing_pipeline.api.middleware import audit_event
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.log import logger
from dubbing_pipeline.web.routes.jobs_common import (
    _apply_transcript_updates,
    _enforce_rate_limit,
    _get_store,
    _job_base_dir,
    _load_transcript_store,
)

from .helpers import (
    _ensure_review_state,
    _hash_text,
    _review_state_path,
    _rewrite_helper_formal,
    _rewrite_helper_reduce_slang,
    _segments_from_state,
)


async def get_job_segments(
    request: Request, id: str, ident: Identity
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id, allow_shared_read=True)
    base_dir = _job_base_dir(job)
    state = _ensure_review_state(base_dir, job.video_path)
    transcript_store = _load_transcript_store(base_dir)
    qa_by_segment: dict[int, dict[str, Any]] = {}
    try:
        rows = store.list_qa_reviews(job_id=str(id))
        qa_by_segment = {
            int(r.get("segment_id") or 0): dict(r)
            for r in rows
            if isinstance(r, dict) and int(r.get("segment_id") or 0) > 0
        }
    except Exception:
        qa_by_segment = {}
    items = _segments_from_state(
        state=state, transcript_store=transcript_store, qa_by_segment=qa_by_segment
    )
    return {
        "items": items,
        "total": len(items),
        "transcript_version": int(transcript_store.get("version") or 0),
    }


async def patch_job_segment(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity,
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    new_text = None
    if "translated_text" in body:
        new_text = str(body.get("translated_text") or "")
    elif "tgt_text" in body:
        new_text = str(body.get("tgt_text") or "")

    pron_overrides = body.get("pronunciation_overrides") if "pronunciation_overrides" in body else None
    glossary_used = body.get("glossary_used") if "glossary_used" in body else None
    notes = body.get("notes") if "notes" in body else None

    version = int(_load_transcript_store(base_dir).get("version") or 0)
    if new_text is not None:
        version, applied = _apply_transcript_updates(
            base_dir=base_dir,
            updates=[{"index": int(segment_id), "tgt_text": new_text, "approved": False}],
        )
        if applied:
            rt = dict(job.runtime or {})
            rt["transcript_version"] = int(version)
            store.update(id, runtime=rt)
        with suppress(Exception):
            from dubbing_pipeline.review.ops import edit_segment

            edit_segment(base_dir, int(segment_id), text=new_text)

    try:
        store.upsert_qa_review(
            job_id=str(id),
            segment_id=int(segment_id),
            status="pending" if new_text is not None else None,
            notes=str(notes) if notes is not None else None,
            edited_text=new_text if new_text is not None else None,
            pronunciation_overrides=pron_overrides,
            glossary_used=glossary_used,
            created_by=str(ident.user.id),
        )
    except Exception as ex:
        raise HTTPException(status_code=400, detail=f"Failed to update QA review: {ex}") from ex

    if new_text is not None:
        logger.info(
            "qa_segment_edit",
            job_id=str(id),
            segment_id=int(segment_id),
            text_hash=_hash_text(new_text),
            has_pron=bool(pron_overrides is not None),
            has_glossary=bool(glossary_used is not None),
        )
    else:
        logger.info(
            "qa_segment_update",
            job_id=str(id),
            segment_id=int(segment_id),
            has_pron=bool(pron_overrides is not None),
            has_glossary=bool(glossary_used is not None),
        )
    audit_event(
        "qa.segment.edit",
        request=request,
        user_id=ident.user.id,
        meta={
            "job_id": id,
            "segment_id": int(segment_id),
            "text_hash": _hash_text(new_text) if new_text is not None else None,
        },
    )
    return {"ok": True, "version": int(version)}


async def post_job_segment_approve(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity,
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    body = {}
    if "application/json" in (request.headers.get("content-type") or "").lower():
        with suppress(Exception):
            body = await request.json()
    notes = body.get("notes") if isinstance(body, dict) else None

    version, applied = _apply_transcript_updates(
        base_dir=base_dir, updates=[{"index": int(segment_id), "approved": True}]
    )
    if applied:
        rt = dict(job.runtime or {})
        rt["transcript_version"] = int(version)
        store.update(id, runtime=rt)
    try:
        store.upsert_qa_review(
            job_id=str(id),
            segment_id=int(segment_id),
            status="approved",
            notes=str(notes) if notes is not None else None,
            created_by=str(ident.user.id),
        )
    except Exception as ex:
        raise HTTPException(status_code=400, detail=f"Failed to update QA review: {ex}") from ex

    logger.info("qa_segment_approve", job_id=str(id), segment_id=int(segment_id))
    audit_event(
        "qa.segment.approve",
        request=request,
        user_id=ident.user.id,
        meta={"job_id": id, "segment_id": int(segment_id)},
    )
    return {"ok": True, "status": "approved", "version": int(version)}


async def post_job_segment_reject(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity,
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    body = {}
    if "application/json" in (request.headers.get("content-type") or "").lower():
        with suppress(Exception):
            body = await request.json()
    notes = body.get("notes") if isinstance(body, dict) else None

    version, applied = _apply_transcript_updates(
        base_dir=base_dir, updates=[{"index": int(segment_id), "approved": False}]
    )
    if applied:
        rt = dict(job.runtime or {})
        rt["transcript_version"] = int(version)
        store.update(id, runtime=rt)
    try:
        store.upsert_qa_review(
            job_id=str(id),
            segment_id=int(segment_id),
            status="rejected",
            notes=str(notes) if notes is not None else None,
            created_by=str(ident.user.id),
        )
    except Exception as ex:
        raise HTTPException(status_code=400, detail=f"Failed to update QA review: {ex}") from ex

    logger.info("qa_segment_reject", job_id=str(id), segment_id=int(segment_id))
    audit_event(
        "qa.segment.reject",
        request=request,
        user_id=ident.user.id,
        meta={"job_id": id, "segment_id": int(segment_id)},
    )
    return {"ok": True, "status": "rejected", "version": int(version)}


async def get_job_review_segments(
    request: Request, id: str, ident: Identity
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    rsp = _review_state_path(base_dir)
    if not rsp.exists():
        try:
            from dubbing_pipeline.review.ops import init_review

            init_review(base_dir, video_path=Path(job.video_path) if job.video_path else None)
        except Exception as ex:
            raise HTTPException(status_code=400, detail=f"review init failed: {ex}") from ex

    from dubbing_pipeline.review.state import load_state

    return load_state(base_dir)


async def post_job_review_helper(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity,
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
        key=f"review:helper:user:{ident.user.id}",
        limit=120,
        per_seconds=60,
    )
    job = require_job_access(store=store, ident=ident, job_id=id)
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
            from dubbing_pipeline.review.state import find_segment, load_state

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
        from dubbing_pipeline.text.pg_filter import apply_pg_filter, built_in_policy

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
        from dubbing_pipeline.timing.fit_text import estimate_speaking_seconds
        from dubbing_pipeline.timing.rewrite_provider import fit_with_rewrite_provider

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
            user_id=ident.user.id,
            meta={
                "job_id": id,
                "segment_id": int(segment_id),
                "kind": kind,
                "provider": provider_used,
            },
        )
    return {"ok": True, "kind": kind, "provider_used": provider_used, "text": out}


async def post_job_review_edit(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity,
) -> dict[str, Any]:
    store = _get_store(request)
    _enforce_rate_limit(
        request,
        key=f"review:edit:user:{ident.user.id}",
        limit=120,
        per_seconds=60,
    )
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    body = await request.json()
    text = str(body.get("text") or "")
    from dubbing_pipeline.review.ops import edit_segment

    try:
        edit_segment(base_dir, int(segment_id), text=text)
        audit_event(
            "review.edit",
            request=request,
            user_id=ident.user.id,
            meta={"job_id": id, "segment_id": int(segment_id)},
        )
        return {"ok": True}
    except Exception as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex


async def post_job_review_lock(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity,
) -> dict[str, Any]:
    store = _get_store(request)
    _enforce_rate_limit(
        request,
        key=f"review:lock:user:{ident.user.id}",
        limit=120,
        per_seconds=60,
    )
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    from dubbing_pipeline.review.ops import lock_segment

    try:
        lock_segment(base_dir, int(segment_id))
        audit_event(
            "review.lock",
            request=request,
            user_id=ident.user.id,
            meta={"job_id": id, "segment_id": int(segment_id)},
        )
        return {"ok": True}
    except Exception as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex


async def post_job_review_unlock(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity,
) -> dict[str, Any]:
    store = _get_store(request)
    _enforce_rate_limit(
        request,
        key=f"review:unlock:user:{ident.user.id}",
        limit=120,
        per_seconds=60,
    )
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    from dubbing_pipeline.review.ops import unlock_segment

    try:
        unlock_segment(base_dir, int(segment_id))
        audit_event(
            "review.unlock",
            request=request,
            user_id=ident.user.id,
            meta={"job_id": id, "segment_id": int(segment_id)},
        )
        return {"ok": True}
    except Exception as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex
