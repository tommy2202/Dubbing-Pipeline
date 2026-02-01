from __future__ import annotations

import time
from contextlib import suppress
from pathlib import Path
from typing import Any, TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request

from dubbing_pipeline.api.deps import Identity, require_scope
from dubbing_pipeline.api.middleware import audit_event
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.limits import get_limits
from dubbing_pipeline.jobs.models import Job, JobState, new_id, now_utc
from dubbing_pipeline.ops.storage import ensure_free_space
from dubbing_pipeline.queue.submit_helpers import submit_job_or_503
from dubbing_pipeline.security import policy, quotas
from dubbing_pipeline.utils.log import request_id_var
from dubbing_pipeline.utils.ratelimit import RateLimiter
from dubbing_pipeline.web.routes._helpers import parse_library_metadata_or_422
from dubbing_pipeline.web.routes.jobs_common import (
    _ALLOWED_UPLOAD_EXTS,
    _ALLOWED_UPLOAD_MIME,
    _app_root,
    _client_ip_for_limits,
    _get_store,
    _input_dir,
    _input_uploads_dir,
    _sanitize_output_subdir,
    _sanitize_video_path,
    _validate_media_or_400,
)

if TYPE_CHECKING:
    from dubbing_pipeline.security.quotas import JobReservation, StorageReservation

router = APIRouter()


@router.post("/api/jobs/batch")
async def create_jobs_batch(
    request: Request, ident: Identity = Depends(require_scope("submit:job"))
) -> dict[str, Any]:
    """
    Batch submit jobs.

    JSON body:
      - items: [{video_path|filename, preset_id?, project_id?}, ...]
      - series_title, series_slug?, season_number?, episode_number?
      - mode, device, src_lang, tgt_lang
    """
    store = _get_store(request)

    # Rate limits
    rl: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if rl is None:
        rl = RateLimiter()
        request.app.state.rate_limiter = rl
    if not rl.allow(f"jobs:batch:user:{ident.user.id}", limit=10, per_seconds=60):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    ip = _client_ip_for_limits(request)
    if not rl.allow(f"jobs:batch:ip:{ip}", limit=10, per_seconds=60):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

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
                "series_slug": series_slug,
                "season_number": int(season_number),
                "episode_number": int(episode_number),
                "user_role": str(getattr(ident.user.role, "value", "") or ""),
            },
        )
        return jid

    limits = get_limits()
    storage_reservations: list[StorageReservation] = []
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON")
        items = body.get("items")
        if not isinstance(items, list) or not items:
            raise HTTPException(status_code=400, detail="items is required")
        total_items = len(items)
        daily_reservation = await policy.reserve_daily_jobs(
            request=request,
            user=ident.user,
            count=total_items,
            action="jobs.batch",
        )
        base_series_title, base_series_slug, base_season, base_episode = (
            parse_library_metadata_or_422(body)
        )
        preset_id = str(body.get("preset_id") or "").strip()
        project_id = str(body.get("project_id") or "").strip()
        preset = store.get_preset(preset_id) if preset_id else None
        project = store.get_project(project_id) if project_id else None
        if project is not None:
            root = _app_root()
            try:
                rel_input = _input_dir().resolve().relative_to(root)
            except Exception:
                rel_input = Path("Input")
            output_subdir = str(project.get("output_subdir") or "").strip()
            project["output_subdir"] = _sanitize_output_subdir(
                output_subdir, default=str(project.get("id") or "project")
            )
            project["input_subdir"] = str(project.get("input_subdir") or "").strip()
            project["input_root"] = str(rel_input)
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
                        code="submission_policy_limit",
                        limit=None,
                        remaining=None,
                        reset_seconds=0,
                        detail=str(pol.detail),
                    )
                raise HTTPException(status_code=int(pol.status_code), detail=str(pol.detail))
            mode = str(getattr(pol, "effective_mode", None) or body.get("mode") or "medium")
            device = str(
                getattr(pol, "effective_device", None) or body.get("device") or "auto"
            )
            src_lang = str(body.get("src_lang") or (preset.get("src_lang") if preset else "ja"))
            tgt_lang = str(body.get("tgt_lang") or (preset.get("tgt_lang") if preset else "en"))
            pg = str(body.get("pg") or "off")
            pg_policy_path = str(body.get("pg_policy_path") or "")
            qa = bool(body.get("qa") or False)
            cache_policy = str(body.get("cache_policy") or "full")
            project_name = str(body.get("project") or body.get("project_name") or "")
            style_guide_path = str(body.get("style_guide_path") or "")

            for it in items:
                if not isinstance(it, dict):
                    raise HTTPException(status_code=400, detail="Invalid items entry")
                vp = it.get("video_path") or it.get("filename")
                if not vp:
                    raise HTTPException(status_code=400, detail="Missing video_path/filename")
                video_path = _sanitize_video_path(str(vp))
                if not video_path.exists() or not video_path.is_file():
                    root = _app_root()
                    try:
                        rel_input = _input_dir().resolve().relative_to(root)
                    except Exception:
                        rel_input = Path("Input")
                    if project is not None:
                        vp = _sanitize_video_path(str(Path(rel_input) / vp))
                    else:
                        vp = _sanitize_video_path(str(Path("Input") / vp))
                    video_path = vp
                if not video_path.exists() or not video_path.is_file():
                    raise HTTPException(status_code=400, detail=f"video_path does not exist: {vp}")

                # Optional per-item metadata override
                meta_payload = {}
                for k in [
                    "series_title",
                    "series_slug",
                    "season_number",
                    "episode_number",
                    "season_text",
                    "episode_text",
                ]:
                    if k in it:
                        meta_payload[k] = it.get(k)
                series_title, series_slug, season_number, episode_number = (
                    parse_library_metadata_or_422(meta_payload)
                    if meta_payload
                    else (base_series_title, base_series_slug, base_season, base_episode)
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
        base_series_title, base_series_slug, base_season, base_episode = (
            parse_library_metadata_or_422(dict(form))
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
                        code="submission_policy_limit",
                        limit=None,
                        remaining=None,
                        reset_seconds=0,
                        detail=str(pol.detail),
                    )
                raise HTTPException(status_code=int(pol.status_code), detail=str(pol.detail))
            mode = str(getattr(pol, "effective_mode", None) or form.get("mode") or "medium")
            device = str(
                getattr(pol, "effective_device", None) or form.get("device") or "auto"
            )
            src_lang = str(form.get("src_lang") or "ja")
            tgt_lang = str(form.get("tgt_lang") or "en")
            pg = str(form.get("pg") or "off")
            pg_policy_path = str(form.get("pg_policy_path") or "")
            qa = str(form.get("qa") or "").strip() not in {"", "0", "false", "off"}
            cache_policy = str(form.get("cache_policy") or "full")
            project_name = str(form.get("project") or form.get("project_name") or "")
            style_guide_path = str(form.get("style_guide_path") or "")

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
                        preset=None,
                        project=None,
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

    audit_event(
        "jobs.batch.submit",
        request=request,
        user_id=ident.user.id,
        meta={"count": len(created_ids)},
    )
    return {"ok": True, "job_ids": created_ids}
