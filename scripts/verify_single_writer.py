from __future__ import annotations

import multiprocessing as mp
import sys
import tempfile
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


def _run_processes(target, args1, args2) -> bool:
    ctx = mp.get_context("spawn")
    p1 = ctx.Process(target=target, args=args1)
    p2 = ctx.Process(target=target, args=args2)
    p1.start()
    p2.start()
    p1.join(30)
    p2.join(30)
    return p1.exitcode == 0 and p2.exitcode == 0


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        job_db = root / "jobs.db"
        auth_db = root / "auth.db"

        ok_jobs = _run_processes(_write_jobs, (str(job_db), "a", 25), (str(job_db), "b", 25))
        ok_auth = _run_processes(_write_users, (str(auth_db), "a", 20), (str(auth_db), "b", 20))

        if not (ok_jobs and ok_auth):
            print("FAIL: writer processes exited non-zero")
            return 1

        if not JobStore(job_db).list(limit=100):
            print("FAIL: job store empty after writes")
            return 1

        if AuthStore(auth_db).get_user_by_username("a-user-0") is None:
            print("FAIL: auth store missing expected user")
            return 1

    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
