from __future__ import annotations

import time
from contextlib import suppress
from pathlib import Path
from typing import Any, TYPE_CHECKING

from fastapi import Depends, HTTPException, Request

from dubbing_pipeline.api.access import require_job_access, require_upload_access
from dubbing_pipeline.api.deps import Identity, require_scope
from dubbing_pipeline.api.middleware import audit_event
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.limits import get_limits
from dubbing_pipeline.jobs.models import Job, JobState, normalize_visibility, new_id, now_utc
from dubbing_pipeline.ops.metrics import jobs_queued, pipeline_job_total
from dubbing_pipeline.queue.submit_helpers import submit_job_or_503
from dubbing_pipeline.ops.storage import ensure_free_space
from dubbing_pipeline.runtime import lifecycle
from dubbing_pipeline.security.crypto import is_encrypted_path, materialize_decrypted
from dubbing_pipeline.security import policy, quotas
from dubbing_pipeline.security.policy_deps import secure_router
from dubbing_pipeline.utils.ffmpeg_safe import FFmpegError
from dubbing_pipeline.utils.log import request_id_var
from dubbing_pipeline.utils.ratelimit import RateLimiter
from dubbing_pipeline.web.routes.jobs_common import (
    _ALLOWED_UPLOAD_EXTS,
    _ALLOWED_UPLOAD_MIME,
    _app_root,
    _client_ip_for_limits,
    _enforce_rate_limit,
    _get_store,
    _input_dir,
    _input_imports_dir,
    _input_uploads_dir,
    _new_short_id,
    _sanitize_output_subdir,
    _sanitize_video_path,
    _validate_media_or_400,
)

if TYPE_CHECKING:
    from dubbing_pipeline.security.quotas import JobReservation, StorageReservation


router = secure_router()

_MAX_IMPORT_TEXT_BYTES = 2 * 1024 * 1024  # 2MB per imported text file (SRT/JSON)


def _parse_library_metadata_or_422(payload: dict[str, Any]) -> tuple[str, str, int, int]:
    """
    Parse required library metadata for job submission.
    Backwards-compatible for old persisted jobs, but NEW submissions must include:
      - series_title (non-empty)
      - season (parseable int >= 1)
      - episode (parseable int >= 1)
    """
    from dubbing_pipeline.library.normalize import normalize_series_title, parse_int_strict, series_to_slug

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
            if (time.time() - ts) <= ttl:
                job = store.get(jid)
                if job is not None:
                    require_job_access(store=store, ident=ident, job=job)
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
        series_title, series_slug, season_number, episode_number = _parse_library_metadata_or_422(
            body
        )
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
        series_title, series_slug, season_number, episode_number = _parse_library_metadata_or_422(
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
            try:
                store.put_upload(
                    direct_upload_id,
                    {
                        "id": direct_upload_id,
                        "owner_id": ident.user.id,
                        "filename": direct_upload_name or direct_upload_path.name,
                        "orig_stem": Path(direct_upload_name or direct_upload_path.name).stem,
                        "total_bytes": int(direct_upload_bytes or file_size),
                        "chunk_bytes": 0,
                        "part_path": "",
                        "final_path": str(direct_upload_path),
                        "received": {},
                        "received_bytes": int(direct_upload_bytes or file_size),
                        "completed": True,
                        "encrypted": bool(is_encrypted_path(direct_upload_path)),
                        "created_at": now_utc(),
                        "updated_at": now_utc(),
                        "source": "direct_job",
                    },
                )
                store.set_upload_storage_bytes(
                    direct_upload_id,
                    user_id=str(ident.user.id),
                    bytes_count=int(direct_upload_bytes or file_size),
                )
            except Exception:
                pass
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
        if privacy_mode:
            rt["privacy_mode"] = str(privacy_mode).strip().lower()
        if bool(no_store_transcript):
            rt["no_store_transcript"] = True
        if bool(no_store_source_audio):
            rt["no_store_source_audio"] = True
        if bool(minimal_artifacts):
            rt["minimal_artifacts"] = True

        # Remember upload metadata (if resumable upload flow was used).
        if upload_id:
            rt["upload_id"] = str(upload_id)
            if upload_stem:
                rt["upload_stem"] = str(upload_stem)

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
    limits = get_limits()
    storage_reservations: list[StorageReservation] = []
    daily_reservation: JobReservation | None = None
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
        try:
            file_size = int(video_path.stat().st_size)
        except Exception:
            file_size = 0
        await policy.require_quota_for_upload(
            request=request,
            user=ident.user,
            bytes=int(file_size),
            action="jobs.batch",
        )
        storage_res = await policy.reserve_storage_bytes(
            request=request,
            user=ident.user,
            bytes_count=int(file_size),
            action="jobs.batch",
        )
        storage_reservations.append(storage_res)
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
        await submit_job_or_503(
            request,
            job_id=jid,
            user_id=str(ident.user.id),
            mode=str(mode),
            device=str(device),
            priority=100,
            meta={
                "series_slug": str(series_slug),
                "season_number": int(season_number),
                "episode_number": int(episode_number),
                "user_role": str(getattr(ident.user.role, "value", "") or ""),
            },
        )
        jobs_queued.inc()
        pipeline_job_total.inc()
        return jid

    ctype = (request.headers.get("content-type") or "").lower()
    await policy.require_concurrent_jobs(request=request, user=ident.user, action="jobs.batch")
    if "application/json" in ctype:
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        total_items = len(body.get("items") or [])
        daily_reservation = await policy.reserve_daily_jobs(
            request=request,
            user=ident.user,
            count=total_items,
            action="jobs.batch",
        )
        base_series_title, base_series_slug, base_season, base_episode = _parse_library_metadata_or_422(
            body
        )
        try:
            pol = await policy.apply_submission_policy(
                request=request,
                user=ident.user,
                requested_mode=str(body.get("mode") or "medium"),
                requested_device=str(body.get("device") or "auto"),
                job_id=None,
            )
            if pol is not None and not pol.ok:
                if int(pol.status_code) == 429:
                    quotas.raise_quota_exceeded(
                        user=ident.user,
                        action="jobs.batch",
                        code="submission_policy_limit",
                        detail=str(pol.detail),
                    )
                raise HTTPException(status_code=int(pol.status_code), detail=str(pol.detail))
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
                # Apply policy adjustments (GPU/mode downgrade) per item.
                pol2 = await policy.apply_submission_policy(
                    request=request,
                    user=ident.user,
                    requested_mode=mode,
                    requested_device=device,
                    job_id=None,
                )
                if pol2 is not None and not pol2.ok:
                    if int(pol2.status_code) == 429:
                        quotas.raise_quota_exceeded(
                            user=ident.user,
                            action="jobs.batch",
                            code="submission_policy_limit",
                            detail=str(pol2.detail),
                        )
                    raise HTTPException(status_code=int(pol2.status_code), detail=str(pol2.detail))
                mode = str(getattr(pol2, "effective_mode", None) or mode)
                device = str(getattr(pol2, "effective_device", None) or device)
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
        except Exception:
            if daily_reservation is not None:
                await daily_reservation.release(count=max(0, total_items - len(created_ids)))
            raise
        finally:
            for res in storage_reservations:
                await res.release()
    else:
        form = await request.form()
        files = form.getlist("files") if hasattr(form, "getlist") else []
        if not files:
            raise HTTPException(status_code=400, detail="Provide files")
        total_items = len(files)
        daily_reservation = await policy.reserve_daily_jobs(
            request=request,
            user=ident.user,
            count=total_items,
            action="jobs.batch",
        )
        base_series_title, base_series_slug, base_season, base_episode = _parse_library_metadata_or_422(
            dict(form)
        )
        try:
            pol = await policy.apply_submission_policy(
                request=request,
                user=ident.user,
                requested_mode=str(form.get("mode") or "medium"),
                requested_device=str(form.get("device") or "auto"),
                job_id=None,
            )
            if pol is not None and not pol.ok:
                if int(pol.status_code) == 429:
                    quotas.raise_quota_exceeded(
                        user=ident.user,
                        action="jobs.batch",
                        code="submission_policy_limit",
                        detail=str(pol.detail),
                    )
                raise HTTPException(status_code=int(pol.status_code), detail=str(pol.detail))
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
            pol2 = await policy.apply_submission_policy(
                request=request,
                user=ident.user,
                requested_mode=mode,
                requested_device=device,
                job_id=None,
            )
            if pol2 is not None and not pol2.ok:
                if int(pol2.status_code) == 429:
                    quotas.raise_quota_exceeded(
                        user=ident.user,
                        action="jobs.batch",
                        code="submission_policy_limit",
                        detail=str(pol2.detail),
                    )
                raise HTTPException(status_code=int(pol2.status_code), detail=str(pol2.detail))
            mode = str(getattr(pol2, "effective_mode", None) or mode)
            device = str(getattr(pol2, "effective_device", None) or device)
            src_lang = str(form.get("src_lang") or (preset.get("src_lang") if preset else "ja"))
            tgt_lang = str(form.get("tgt_lang") or (preset.get("tgt_lang") if preset else "en"))
            pg = str(form.get("pg") or "off")
            pg_policy_path = str(form.get("pg_policy_path") or "")
            qa = str(form.get("qa") or "").strip() not in {"", "0", "false", "off"}
            cache_policy = str(form.get("cache_policy") or "full")

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
                                action="jobs.batch.upload",
                            )
                            f.write(chunk)
                    storage_res = await policy.reserve_storage_bytes(
                        request=request,
                        user=ident.user,
                        bytes_count=int(written),
                        action="jobs.batch",
                    )
                    storage_reservations.append(storage_res)
                except Exception:
                    with suppress(Exception):
                        dest.unlink(missing_ok=True)
                    raise
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
        except Exception:
            if daily_reservation is not None:
                await daily_reservation.release(count=max(0, total_items - len(created_ids)))
            raise
        finally:
            for res in storage_reservations:
                await res.release()

    return {"ids": created_ids}
