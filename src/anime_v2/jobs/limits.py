from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime

from anime_v2.jobs.models import Job, JobState


@dataclass(frozen=True, slots=True)
class Limits:
    max_video_min: int = 120
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


def get_limits() -> Limits:
    def _i(name: str, default: int) -> int:
        try:
            v = int(os.environ.get(name, str(default)))
            return max(0, v)
        except Exception:
            return default

    return Limits(
        max_video_min=_i("MAX_VIDEO_MIN", 120),
        max_upload_mb=_i("MAX_UPLOAD_MB", 2048),
        max_concurrent_per_user=_i("MAX_CONCURRENT", 2),
        daily_processing_minutes=_i("DAILY_PROCESSING_MINUTES", 240),
        timeout_audio_s=_i("WATCHDOG_AUDIO_S", 10 * 60),
        timeout_diarize_s=_i("WATCHDOG_DIARIZE_S", 20 * 60),
        timeout_whisper_s=_i("WATCHDOG_WHISPER_S", 45 * 60),
        timeout_translate_s=_i("WATCHDOG_TRANSLATE_S", 10 * 60),
        timeout_tts_s=_i("WATCHDOG_TTS_S", 30 * 60),
        timeout_mix_s=_i("WATCHDOG_MIX_S", 20 * 60),
    )


def _same_utc_day(a_iso: str, b_iso: str) -> bool:
    try:
        a = datetime.fromisoformat(a_iso.replace("Z", "+00:00")).astimezone(UTC).date()
        b = datetime.fromisoformat(b_iso.replace("Z", "+00:00")).astimezone(UTC).date()
        return a == b
    except Exception:
        return False


def concurrent_jobs_for_user(jobs: list[Job], *, user_id: str) -> int:
    return sum(1 for j in jobs if j.owner_id == user_id and j.state in {JobState.QUEUED, JobState.RUNNING})


def used_minutes_today(jobs: list[Job], *, user_id: str, now_iso: str) -> float:
    total_s = 0.0
    for j in jobs:
        if j.owner_id != user_id:
            continue
        if not _same_utc_day(j.created_at, now_iso):
            continue
        total_s += float(j.duration_s or 0.0)
    return total_s / 60.0

