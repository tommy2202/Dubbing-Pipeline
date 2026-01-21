from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

from dubbing_pipeline.api.models import AuthStore, Role, User
from dubbing_pipeline.jobs.models import Job, JobState, now_utc
from dubbing_pipeline.jobs.store import JobStore


def _write_jobs(db_path: str, prefix: str, count: int) -> None:
    store = JobStore(Path(db_path))
    for i in range(count):
        job_id = f"{prefix}-{i}"
        job = Job(
            id=job_id,
            owner_id=f"user-{prefix}",
            video_path="Input/example.mp4",
            duration_s=1.0,
            mode="standard",
            device="cpu",
            src_lang="en",
            tgt_lang="es",
            created_at=now_utc(),
            updated_at=now_utc(),
            state=JobState.QUEUED,
            progress=0.0,
            message="queued",
            output_mkv="",
            output_srt="",
            work_dir="",
            log_path="",
        )
        store.put(job)
        if i % 5 == 0:
            store.update(job_id, progress=float(i))
        time.sleep(0.005)


def _write_users(db_path: str, prefix: str, count: int) -> None:
    store = AuthStore(Path(db_path))
    for i in range(count):
        user = User(
            id=f"{prefix}-{i}",
            username=f"{prefix}-user-{i}",
            password_hash="hash",
            role=Role.viewer,
            totp_secret=None,
            totp_enabled=False,
            created_at=int(time.time()),
        )
        store.upsert_user(user)
        time.sleep(0.005)


def test_single_writer_lock_jobstore(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    ctx = mp.get_context("spawn")
    p1 = ctx.Process(target=_write_jobs, args=(str(db_path), "a", 25))
    p2 = ctx.Process(target=_write_jobs, args=(str(db_path), "b", 25))
    p1.start()
    p2.start()
    p1.join(30)
    p2.join(30)
    assert p1.exitcode == 0
    assert p2.exitcode == 0

    store = JobStore(db_path)
    jobs = store.list(limit=100)
    assert len(jobs) >= 2


def test_single_writer_lock_authstore(tmp_path: Path) -> None:
    db_path = tmp_path / "auth.db"
    ctx = mp.get_context("spawn")
    p1 = ctx.Process(target=_write_users, args=(str(db_path), "a", 20))
    p2 = ctx.Process(target=_write_users, args=(str(db_path), "b", 20))
    p1.start()
    p2.start()
    p1.join(30)
    p2.join(30)
    assert p1.exitcode == 0
    assert p2.exitcode == 0

    store = AuthStore(db_path)
    assert store.get_user_by_username("a-user-0") is not None
