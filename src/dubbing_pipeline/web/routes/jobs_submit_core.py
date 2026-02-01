from __future__ import annotations

import time
from contextlib import suppress
from pathlib import Path
from typing import Any, TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request

from dubbing_pipeline.api.access import require_job_access, require_upload_access
from dubbing_pipeline.api.deps import Identity, require_scope
from dubbing_pipeline.api.middleware import audit_event
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.limits import get_limits
from dubbing_pipeline.jobs.models import Job, JobState, normalize_visibility, new_id, now_utc
from dubbing_pipeline.ops.metrics import jobs_queued, pipeline_job_total
from dubbing_pipeline.ops.storage import ensure_free_space
from dubbing_pipeline.queue.submit_helpers import submit_job_or_503
from dubbing_pipeline.runtime import lifecycle
from dubbing_pipeline.security import policy, quotas
from dubbing_pipeline.security.crypto import is_encrypted_path, materialize_decrypted
from dubbing_pipeline.utils.ffmpeg_safe import FFmpegError
from dubbing_pipeline.utils.log import request_id_var
from dubbing_pipeline.utils.ratelimit import RateLimiter
from dubbing_pipeline.web.routes._helpers import parse_library_metadata_or_422
from dubbing_pipeline.web.routes.jobs_common import (
    _ALLOWED_UPLOAD_EXTS,
    _ALLOWED_UPLOAD_MIME,
    _client_ip_for_limits,
    _get_store,
    _input_imports_dir,
    _input_uploads_dir,
    _new_short_id,
    _sanitize_video_path,
    _validate_media_or_400,
)
from dubbing_pipeline.web.routes.jobs_submit_upload import record_direct_upload

if TYPE_CHECKING:
    from dubbing_pipeline.security.quotas import JobReservation, StorageReservation

router = APIRouter()

_MAX_IMPORT_TEXT_BYTES = 2 * 1024 * 1024  # 2MB per imported text file (SRT/JSON)


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
        try:
            existing = store.get_idempotency(idem_key)
            if existing:
                jid, _ts = existing
                job = store.get(jid)
                if job is not None:
                    require_job_access(store=store, ident=ident, job=job)
                    return {"id": str(jid)}
        except Exception:
            pass

    if lifecycle.is_draining():
        ra = str(lifecycle.retry_after_seconds(60))
        raise HTTPException(
            status_code=503,
            detail="System is draining; try again later",
            headers={"Retry-After": ra},
        )

    # Basic rate limit: per-user + per-IP for job submit
    rl: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if rl is None:
        rl = RateLimiter()
        request.app.state.rate_limiter = rl
    if not rl.allow(f"jobs:submit:user:{ident.user.id}", limit=10, per_seconds=60):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    ip = _client_ip_for_limits(request)
    if not rl.allow(f"jobs:submit:ip:{ip}", limit=25, per_seconds=60):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    # Disk guard: refuse new jobs when storage is low.
    s = get_settings()
    out_root = Path(str(getattr(store, "db_path", Path(s.output_dir)))).resolve().parent
    out_root.mkdir(parents=True, exist_ok=True)
    ensure_free_space(min_gb=int(s.min_free_gb), path=out_root)

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
    visibility = "private"

    upload_id = ""
    upload_stem = ""
    skip_upload_quota = False
    direct_upload_id = ""
    direct_upload_bytes = 0
    direct_upload_name = ""
    direct_upload_path: Path | None = None
    import_src_srt_text: str | None = None
    import_tgt_srt_text: str | None = None
    import_transcript_json_text: str | None = None

    if "application/json" in ctype:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON")
        # Library metadata is required for new jobs (validate early).
        series_title, series_slug, season_number, episode_number = parse_library_metadata_or_422(body)
        visibility = str(body.get("visibility") or visibility)
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
            urec = require_upload_access(store=store, ident=ident, upload_id=upload_id)
            if not bool(urec.get("completed")):
                raise HTTPException(status_code=400, detail="upload_id not completed")
            skip_upload_quota = True
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
        series_title, series_slug, season_number, episode_number = parse_library_metadata_or_422(
            dict(form)
        )
        visibility = str(form.get("visibility") or visibility)
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
        vp = form.get("video_path")
        file = form.get("file")
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
            urec = require_upload_access(store=store, ident=ident, upload_id=upload_id)
            if not bool(urec.get("completed")):
                raise HTTPException(status_code=400, detail="upload_id not completed")
            skip_upload_quota = True
            up_root = _input_uploads_dir().resolve()
            video_path = Path(str(urec.get("final_path") or "")).resolve()
            try:
                video_path.relative_to(up_root)
            except Exception:
                raise HTTPException(status_code=400, detail="upload_id path not allowed") from None
            upload_stem = str(urec.get("orig_stem") or "").strip()
        elif vp is not None:
            vp_s = str(vp)
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
            written = 0
            try:
                with dest.open("wb") as f:
                    while True:
                        chunk = await upload.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        await policy.require_upload_progress(
                            request=request,
                            user=ident.user,
                            written_bytes=int(written),
                            action="jobs.upload",
                        )
                        f.write(chunk)
            except HTTPException:
                with suppress(Exception):
                    dest.unlink(missing_ok=True)
                raise
            video_path = dest
            direct_upload_id = _new_short_id("up_direct_")
            direct_upload_bytes = int(written)
            direct_upload_name = str(name or dest.name)
            direct_upload_path = dest

    assert video_path is not None
    jid = new_id()
    storage_reservation: quotas.StorageReservation | None = None
    if store is not None:
        try:
            file_size = int(video_path.stat().st_size)
        except Exception:
            file_size = 0
        if not skip_upload_quota:
            await policy.require_quota_for_upload(
                request=request,
                user=ident.user,
                bytes=int(file_size),
                action="jobs.submit",
            )
        if direct_upload_id and direct_upload_path is not None:
            record_direct_upload(
                store=store,
                upload_id=direct_upload_id,
                user_id=str(ident.user.id),
                video_path=direct_upload_path,
                bytes_written=int(direct_upload_bytes or file_size),
                filename=direct_upload_name or direct_upload_path.name,
            )
        storage_reservation = await policy.reserve_storage_bytes(
            request=request,
            user=ident.user,
            bytes_count=int(file_size),
            action="jobs.submit",
        )
    # Extension allowlist (defense-in-depth). Encrypted-at-rest inputs are allowed (validated via ffprobe after decrypt).
    if not is_encrypted_path(video_path) and video_path.suffix.lower() not in _ALLOWED_UPLOAD_EXTS:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    try:
        reservation = await policy.require_quota_for_submit(
            request=request,
            user=ident.user,
            count=1,
            requested_mode=mode,
            requested_device=device,
            job_id=jid,
            action="jobs.submit",
        )
    except Exception:
        if storage_reservation is not None:
            await storage_reservation.release()
        raise
    # Apply policy adjustments (GPU downgrade / mode downgrade).
    mode = str(reservation.effective_mode or mode)
    device = str(reservation.effective_device or device)
    # Validate using ffprobe (no user-controlled args).
    job_created = False
    try:
        with materialize_decrypted(video_path, kind="uploads", job_id=None, suffix=".input") as mat:
            duration_s = float(_validate_media_or_400(mat.path, limits=limits))
        await policy.require_processing_minutes(
            request=request, user=ident.user, duration_s=duration_s, action="jobs.submit"
        )
    except HTTPException:
        await reservation.release()
        if storage_reservation is not None:
            await storage_reservation.release()
        raise
    except FFmpegError as ex:
        await reservation.release()
        if storage_reservation is not None:
            await storage_reservation.release()
        raise HTTPException(
            status_code=400, detail=f"Invalid media file (ffprobe failed): {ex}"
        ) from ex
    created = now_utc()
    vis_norm = normalize_visibility(visibility)
    policy.require_share_allowed(user=ident.user, visibility_value=vis_norm.value)

    try:
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
            visibility=vis_norm,
        )
        rt = dict(job.runtime or {})
        if upload_id:
            rt["upload_id"] = str(upload_id)
            if upload_stem:
                rt["upload_stem"] = str(upload_stem)
        if project_name:
            rt["project_name"] = str(project_name)
        if style_guide_path:
            rt["style_guide_path"] = str(style_guide_path)
        if privacy_mode:
            rt["privacy"] = str(privacy_mode)
        if no_store_transcript:
            rt["no_store_transcript"] = True
        if no_store_source_audio:
            rt["no_store_source_audio"] = True
        if minimal_artifacts:
            rt["minimal_artifacts"] = True
        if speaker_smoothing:
            rt["speaker_smoothing"] = True
        if scene_detect:
            rt["scene_detect"] = str(scene_detect)
        if director:
            rt["director"] = True
            rt["director_strength"] = float(director_strength)

        # Optional imports (best-effort; store small text files under INPUT_DIR/imports/<job_id>/)
        if import_src_srt_text or import_tgt_srt_text or import_transcript_json_text:
            imp_dir = (_input_imports_dir() / jid).resolve()
            with suppress(Exception):
                imp_dir.mkdir(parents=True, exist_ok=True)
            if import_src_srt_text:
                if len(import_src_srt_text.encode("utf-8", errors="ignore")) > _MAX_IMPORT_TEXT_BYTES:
                    raise HTTPException(status_code=400, detail="src_srt_text too large")
                sp = (imp_dir / "src.srt").resolve()
                sp.write_text(import_src_srt_text, encoding="utf-8")
                rt["import_src_srt_path"] = str(sp)
            if import_tgt_srt_text:
                if len(import_tgt_srt_text.encode("utf-8", errors="ignore")) > _MAX_IMPORT_TEXT_BYTES:
                    raise HTTPException(status_code=400, detail="tgt_srt_text too large")
                tp = (imp_dir / "tgt.srt").resolve()
                tp.write_text(import_tgt_srt_text, encoding="utf-8")
                rt["import_tgt_srt_path"] = str(tp)
            if import_transcript_json_text:
                if (
                    len(import_transcript_json_text.encode("utf-8", errors="ignore"))
                    > _MAX_IMPORT_TEXT_BYTES
                ):
                    raise HTTPException(status_code=400, detail="transcript_json_text too large")
                jp = (imp_dir / "transcript.json").resolve()
                jp.write_text(import_transcript_json_text, encoding="utf-8")
                rt["import_transcript_json_path"] = str(jp)

        job.runtime = rt
        store.put(job)
        job_created = True
    except Exception:
        await reservation.release()
        if storage_reservation is not None:
            await storage_reservation.release()
        raise
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
    # Enqueue via the canonical queue backend (Redis L2 with fallback to local L1).
    try:
        await submit_job_or_503(
            request,
            job_id=jid,
            user_id=str(ident.user.id),
            mode=str(mode),
            device=str(device),
            priority=100,
            meta={
                "series_slug": series_slug,
                "season_number": int(season_number),
                "episode_number": int(episode_number),
                "user_role": str(getattr(ident.user.role, "value", "") or ""),
            },
        )
    except Exception:
        if not job_created:
            await reservation.release()
        if storage_reservation is not None:
            await storage_reservation.release()
        raise
    jobs_queued.inc()
    pipeline_job_total.inc()
    if storage_reservation is not None:
        await storage_reservation.release()
    return {"id": jid}
