from __future__ import annotations

import json
import re
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from dubbing_pipeline.api.access import require_job_access
from dubbing_pipeline.api.deps import Identity, require_scope
from dubbing_pipeline.api.middleware import audit_event
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import JobState, now_utc
from dubbing_pipeline.runtime.scheduler import JobRecord
from dubbing_pipeline.web.routes.jobs_common import (
    _enforce_rate_limit,
    _file_range_response,
    _get_scheduler,
    _get_store,
    _job_base_dir,
    _load_transcript_store,
    _output_root,
    _save_transcript_store,
    _append_transcript_version,
)

router = APIRouter()


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
        from dubbing_pipeline.review.state import load_state

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


@router.get("/api/jobs/{id}/overrides")
async def get_job_overrides(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
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


@router.get("/api/jobs/{id}/overrides/music/effective")
async def get_job_music_regions_effective(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
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


@router.put("/api/jobs/{id}/overrides")
async def put_job_overrides(
    request: Request, id: str, ident: Identity = Depends(require_scope("edit:job"))
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


@router.post("/api/jobs/{id}/overrides/apply")
async def apply_job_overrides(
    request: Request, id: str, ident: Identity = Depends(require_scope("edit:job"))
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


@router.get("/api/jobs/{id}/characters")
async def get_job_characters(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    rt = dict(job.runtime or {})
    items = rt.get("voice_map", [])
    if not isinstance(items, list):
        items = []
    return {"items": items}


@router.put("/api/jobs/{id}/characters")
async def put_job_characters(
    request: Request, id: str, ident: Identity = Depends(require_scope("edit:job"))
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


@router.get("/api/jobs/{id}/transcript")
async def get_job_transcript(
    request: Request,
    id: str,
    page: int = 1,
    per_page: int = 50,
    ident: Identity = Depends(require_scope("read:job")),
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


@router.put("/api/jobs/{id}/transcript")
async def put_job_transcript(
    request: Request, id: str, ident: Identity = Depends(require_scope("edit:job"))
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
        user_id=ident.user.id,
        meta={"job_id": id, "updates": int(len(applied)), "version": int(st["version"])},
    )
    return {"ok": True, "version": st["version"]}


@router.post("/api/jobs/{id}/overrides/speaker")
async def set_speaker_overrides_from_ui(
    request: Request, id: str, ident: Identity = Depends(require_scope("edit:job"))
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


@router.post("/api/jobs/{id}/transcript/synthesize")
async def synthesize_from_approved(
    request: Request, id: str, ident: Identity = Depends(require_scope("edit:job"))
) -> dict[str, Any]:
    store = _get_store(request)
    scheduler = _get_scheduler(request)
    qb = getattr(request.app.state, "queue_backend", None)
    job = require_job_access(store=store, ident=ident, job_id=id)
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
        if qb is not None:
            await qb.submit_job(
                job_id=str(id),
                user_id=str(ident.user.id),
                mode=str((job2.mode if job2 else job.mode)),
                device=str((job2.device if job2 else job.device)),
                priority=50,
                meta={"user_role": str(getattr(ident.user.role, "value", "") or "")},
            )
        else:
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
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
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
    ident: Identity = Depends(require_scope("edit:job")),
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


@router.post("/api/jobs/{id}/review/segments/{segment_id}/edit")
async def post_job_review_edit(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity = Depends(require_scope("edit:job")),
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


@router.post("/api/jobs/{id}/review/segments/{segment_id}/regen")
async def post_job_review_regen(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity = Depends(require_scope("edit:job")),
) -> dict[str, Any]:
    store = _get_store(request)
    _enforce_rate_limit(
        request,
        key=f"review:regen:user:{ident.user.id}",
        limit=60,
        per_seconds=60,
    )
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    from dubbing_pipeline.review.ops import regen_segment

    try:
        p = regen_segment(base_dir, int(segment_id))
        audit_event(
            "review.regen",
            request=request,
            user_id=ident.user.id,
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
    ident: Identity = Depends(require_scope("edit:job")),
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


@router.post("/api/jobs/{id}/review/segments/{segment_id}/unlock")
async def post_job_review_unlock(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity = Depends(require_scope("edit:job")),
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


@router.get("/api/jobs/{id}/review/segments/{segment_id}/audio")
async def get_job_review_audio(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity = Depends(require_scope("read:job")),
) -> Response:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    p = _review_audio_path(base_dir, int(segment_id))
    if p is None:
        raise HTTPException(status_code=404, detail="audio not found")
    return _file_range_response(
        request, p, media_type="audio/wav", allowed_roots=[_job_base_dir(job)]
    )
