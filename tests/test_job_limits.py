from __future__ import annotations

from anime_v2.jobs.limits import concurrent_jobs_for_user, used_minutes_today
from anime_v2.jobs.models import Job, JobState


def _job(*, owner_id: str, created_at: str, duration_s: float, state: JobState) -> Job:
    return Job(
        id="j1",
        owner_id=owner_id,
        video_path="/tmp/x.mp4",
        duration_s=duration_s,
        mode="low",
        device="cpu",
        src_lang="auto",
        tgt_lang="en",
        created_at=created_at,
        updated_at=created_at,
        state=state,
        progress=0.0,
        message="",
        output_mkv="",
        output_srt="",
        work_dir="",
        log_path="",
        error=None,
        request_id="r1",
    )


def test_concurrent_jobs_count() -> None:
    jobs = [
        _job(
            owner_id="u1",
            created_at="2026-01-01T00:00:00+00:00",
            duration_s=60,
            state=JobState.QUEUED,
        ),
        _job(
            owner_id="u1",
            created_at="2026-01-01T00:00:00+00:00",
            duration_s=60,
            state=JobState.RUNNING,
        ),
        _job(
            owner_id="u1",
            created_at="2026-01-01T00:00:00+00:00",
            duration_s=60,
            state=JobState.DONE,
        ),
        _job(
            owner_id="u2",
            created_at="2026-01-01T00:00:00+00:00",
            duration_s=60,
            state=JobState.RUNNING,
        ),
    ]
    assert concurrent_jobs_for_user(jobs, user_id="u1") == 2


def test_used_minutes_today() -> None:
    jobs = [
        _job(
            owner_id="u1",
            created_at="2026-01-01T01:00:00+00:00",
            duration_s=120,
            state=JobState.DONE,
        ),
        _job(
            owner_id="u1",
            created_at="2026-01-01T02:00:00+00:00",
            duration_s=60,
            state=JobState.FAILED,
        ),
        _job(
            owner_id="u1",
            created_at="2026-01-02T02:00:00+00:00",
            duration_s=600,
            state=JobState.DONE,
        ),
        _job(
            owner_id="u2",
            created_at="2026-01-01T03:00:00+00:00",
            duration_s=600,
            state=JobState.DONE,
        ),
    ]
    assert (
        abs(used_minutes_today(jobs, user_id="u1", now_iso="2026-01-01T23:00:00+00:00") - 3.0)
        < 1e-6
    )
