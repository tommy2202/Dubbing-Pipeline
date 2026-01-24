from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState


@dataclass(frozen=True, slots=True)
class Limits:
    max_video_min: int = 120
    max_video_width: int = 0
    max_video_height: int = 0
    max_video_pixels: int = 0
    max_upload_bytes: int = 0
    max_upload_mb: int = 2048  # 2GB
    max_concurrent_per_user: int = 2
    daily_processing_minutes: int = 240  # sum of submitted durations per day

    # phase watchdogs (seconds)
    timeout_audio_s: int = 10 * 60
    timeout_diarize_s: int = 20 * 60
    timeout_whisper_s: int = 45 * 60
    timeout_translate_s: int = 10 * 60
    timeout_tts_s: int = 30 * 60
    timeout_mix_s: int = 20 * 60
    timeout_mux_s: int = 20 * 60
    timeout_export_s: int = 20 * 60


def get_limits() -> Limits:
    s = get_settings()
    max_upload_bytes = max(0, int(getattr(s, "max_upload_bytes", 0) or 0))
    if max_upload_bytes <= 0:
        max_upload_bytes = max(0, int(s.max_upload_mb)) * 1024 * 1024
    return Limits(
        max_video_min=max(0, int(s.max_video_min)),
        max_video_width=max(0, int(getattr(s, "max_video_width", 0))),
        max_video_height=max(0, int(getattr(s, "max_video_height", 0))),
        max_video_pixels=max(0, int(getattr(s, "max_video_pixels", 0))),
        max_upload_bytes=max_upload_bytes,
        max_upload_mb=max(0, int(s.max_upload_mb)),
        max_concurrent_per_user=max(0, int(s.max_concurrent_per_user)),
        daily_processing_minutes=max(0, int(s.daily_processing_minutes)),
        timeout_audio_s=max(0, int(s.watchdog_audio_s)),
        timeout_diarize_s=max(0, int(s.watchdog_diarize_s)),
        timeout_whisper_s=max(0, int(s.watchdog_whisper_s)),
        timeout_translate_s=max(0, int(s.watchdog_translate_s)),
        timeout_tts_s=max(0, int(s.watchdog_tts_s)),
        timeout_mix_s=max(0, int(s.watchdog_mix_s)),
        timeout_mux_s=max(0, int(getattr(s, "watchdog_mux_s", 20 * 60))),
        timeout_export_s=max(0, int(getattr(s, "watchdog_export_s", 20 * 60))),
    )


@dataclass(frozen=True, slots=True)
class Quotas:
    max_upload_bytes: int
    jobs_per_day_per_user: int
    max_concurrent_jobs_per_user: int
    max_storage_bytes_per_user: int


def resolve_user_quotas(
    *, overrides: dict[str, int | None] | None = None
) -> Quotas:
    s = get_settings()
    max_upload_bytes = max(0, int(getattr(s, "max_upload_bytes", 0) or 0))
    if max_upload_bytes <= 0:
        max_upload_bytes = max(0, int(getattr(s, "max_upload_mb", 0) or 0)) * 1024 * 1024
    jobs_per_day = max(0, int(getattr(s, "jobs_per_day_per_user", 0) or 0))
    if jobs_per_day <= 0:
        jobs_per_day = max(0, int(getattr(s, "daily_job_cap", 0) or 0))
    max_concurrent = max(0, int(getattr(s, "max_concurrent_jobs_per_user", 0) or 0))
    if max_concurrent <= 0:
        max_concurrent = max(0, int(getattr(s, "max_active_jobs_per_user", 0) or 0))
    max_storage = max(0, int(getattr(s, "max_storage_bytes_per_user", 0) or 0))

    if isinstance(overrides, dict):
        if overrides.get("max_upload_bytes") is not None:
            with __import__("contextlib").suppress(Exception):
                max_upload_bytes = max(0, int(overrides["max_upload_bytes"] or 0))
        if overrides.get("jobs_per_day") is not None:
            with __import__("contextlib").suppress(Exception):
                jobs_per_day = max(0, int(overrides["jobs_per_day"] or 0))
        if overrides.get("max_concurrent_jobs") is not None:
            with __import__("contextlib").suppress(Exception):
                max_concurrent = max(0, int(overrides["max_concurrent_jobs"] or 0))
        if overrides.get("max_storage_bytes") is not None:
            with __import__("contextlib").suppress(Exception):
                max_storage = max(0, int(overrides["max_storage_bytes"] or 0))

    return Quotas(
        max_upload_bytes=max_upload_bytes,
        jobs_per_day_per_user=jobs_per_day,
        max_concurrent_jobs_per_user=max_concurrent,
        max_storage_bytes_per_user=max_storage,
    )


def _same_utc_day(a_iso: str, b_iso: str) -> bool:
    try:
        a = datetime.fromisoformat(a_iso.replace("Z", "+00:00")).astimezone(timezone.utc).date()
        b = datetime.fromisoformat(b_iso.replace("Z", "+00:00")).astimezone(timezone.utc).date()
        return a == b
    except Exception:
        return False


def concurrent_jobs_for_user(jobs: list[Job], *, user_id: str) -> int:
    return sum(
        1 for j in jobs if j.owner_id == user_id and j.state in {JobState.QUEUED, JobState.RUNNING}
    )


def used_minutes_today(jobs: list[Job], *, user_id: str, now_iso: str) -> float:
    total_s = 0.0
    for j in jobs:
        if j.owner_id != user_id:
            continue
        if not _same_utc_day(j.created_at, now_iso):
            continue
        total_s += float(j.duration_s or 0.0)
    return total_s / 60.0
