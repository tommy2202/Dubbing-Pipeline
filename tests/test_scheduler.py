from __future__ import annotations

import time

import pytest

from anime_v2.jobs.models import Job, JobState
from anime_v2.jobs.store import JobStore
from anime_v2.runtime.scheduler import JobRecord, Scheduler
from anime_v2.runtime import lifecycle


def _mk_job(jid: str, *, owner: str = "u1") -> Job:
    now = "2026-01-01T00:00:00+00:00"
    return Job(
        id=jid,
        owner_id=owner,
        video_path="/tmp/x.mp4",
        duration_s=10.0,
        mode="high",
        device="cpu",
        src_lang="auto",
        tgt_lang="en",
        created_at=now,
        updated_at=now,
        state=JobState.QUEUED,
        progress=0.0,
        message="Queued",
        output_mkv="",
        output_srt="",
        work_dir="",
        log_path="",
        error=None,
        request_id="r1",
    )


def test_backpressure_degrades_mode(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    lifecycle.end_draining()
    # Make backpressure threshold tiny
    monkeypatch.setenv("BACKPRESSURE_Q_MAX", "0")
    monkeypatch.setenv("MAX_CONCURRENCY_GLOBAL", "1")
    monkeypatch.setenv("MAX_CONCURRENCY_TRANSCRIBE", "1")
    monkeypatch.setenv("MAX_CONCURRENCY_TTS", "1")
    from anime_v2.config import get_settings

    get_settings.cache_clear()

    store = JobStore(tmp_path / "jobs.db")
    enq = []

    def enqueue_cb(job: Job):
        enq.append(job.id)

    sched = Scheduler(store=store, enqueue_cb=enqueue_cb)
    Scheduler.install(sched)

    store.put(_mk_job("j1"))
    store.put(_mk_job("j2"))

    # First submission fills the queue (bp_qmax=0 means second submission sees qlen>0).
    sched.submit(JobRecord(job_id="j1", mode="high", device_pref="cpu", created_at=time.time()))
    sched.submit(JobRecord(job_id="j2", mode="high", device_pref="cpu", created_at=time.time()))

    j2 = store.get("j2")
    assert j2 is not None
    assert j2.mode in {"medium", "low"}
    assert bool((j2.runtime or {}).get("metadata", {}).get("degraded")) is True

