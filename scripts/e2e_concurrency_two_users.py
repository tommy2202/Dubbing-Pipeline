from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


def main() -> int:
    """
    Concurrency sanity check (scheduler gating):
    - With MAX_CONCURRENCY_GLOBAL=1, only one job is dispatched at a time.
    - We simulate completion by calling Scheduler.on_job_done().

    This does not run the heavy pipeline; it validates queue/scheduler wiring.
    """

    os.environ["MAX_CONCURRENCY_GLOBAL"] = "1"
    os.environ["MAX_CONCURRENCY_TRANSCRIBE"] = "1"
    os.environ["MAX_CONCURRENCY_TTS"] = "1"

    from anime_v2.jobs.models import Job, JobState, now_utc
    from anime_v2.jobs.store import JobStore
    from anime_v2.runtime.scheduler import JobRecord, Scheduler

    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        store = JobStore(root / "jobs.db")

        dispatched: list[str] = []

        def _enqueue_cb(job: Job) -> None:
            # Scheduler dispatch callback (runs in scheduler thread).
            dispatched.append(str(job.id))

        sched = Scheduler(store=store, enqueue_cb=_enqueue_cb)
        Scheduler.install(sched)
        sched.start()

        try:
            # Two users submit one job each (stored in SQLite).
            j1 = Job(
                id="job_u1",
                owner_id="u1",
                video_path="/dev/null",
                duration_s=1.0,
                mode="low",
                device="cpu",
                src_lang="auto",
                tgt_lang="en",
                created_at=now_utc(),
                updated_at=now_utc(),
                state=JobState.QUEUED,
                progress=0.0,
                message="Queued",
                output_mkv="",
                output_srt="",
                work_dir="",
                log_path="",
                error=None,
            )
            j2 = Job(
                id="job_u2",
                owner_id="u2",
                video_path="/dev/null",
                duration_s=1.0,
                mode="low",
                device="cpu",
                src_lang="auto",
                tgt_lang="en",
                created_at=now_utc(),
                updated_at=now_utc(),
                state=JobState.QUEUED,
                progress=0.0,
                message="Queued",
                output_mkv="",
                output_srt="",
                work_dir="",
                log_path="",
                error=None,
            )
            store.put(j1)
            store.put(j2)

            sched.submit(JobRecord(job_id=j1.id, mode=j1.mode, device_pref=j1.device, created_at=time.time()))
            sched.submit(JobRecord(job_id=j2.id, mode=j2.mode, device_pref=j2.device, created_at=time.time()))

            # Wait for first dispatch.
            t0 = time.time()
            while time.time() - t0 < 5.0 and len(dispatched) < 1:
                time.sleep(0.05)
            assert dispatched == ["job_u1"] or dispatched == ["job_u2"], dispatched

            # With global concurrency=1, second should not dispatch until first is marked done.
            time.sleep(0.25)
            assert len(dispatched) == 1, dispatched

            # Simulate completion.
            sched.on_job_done(dispatched[0])

            t1 = time.time()
            while time.time() - t1 < 5.0 and len(dispatched) < 2:
                time.sleep(0.05)
            assert len(dispatched) == 2, dispatched

            print("e2e_concurrency_two_users: OK")
            return 0
        finally:
            sched.stop()


if __name__ == "__main__":
    raise SystemExit(main())

