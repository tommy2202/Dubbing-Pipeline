#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path


from dubbing_pipeline.jobs.queue import JobQueue


class _NoopJobQueue(JobQueue):
    """
    Queue subclass that avoids running the heavy pipeline.
    """

    async def _worker(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            return


async def _run() -> None:
    from dubbing_pipeline.jobs.models import Job, JobState, now_utc
    from dubbing_pipeline.jobs.store import JobStore

    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        os.environ.setdefault("DUBBING_OUTPUT_DIR", str(root / "Output"))
        os.environ.setdefault("STRICT_SECRETS", "0")

        store = JobStore(root / "jobs.db")
        j1 = Job(
            id="job_running",
            owner_id="u1",
            video_path="/dev/null",
            duration_s=1.0,
            mode="low",
            device="cpu",
            src_lang="auto",
            tgt_lang="en",
            created_at=now_utc(),
            updated_at=now_utc(),
            state=JobState.RUNNING,
            progress=0.1,
            message="Running before crash",
            output_mkv="",
            output_srt="",
            work_dir="",
            log_path="",
            error=None,
        )
        j2 = Job(
            id="job_queued",
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
            message="Queued before crash",
            output_mkv="",
            output_srt="",
            work_dir="",
            log_path="",
            error=None,
        )
        store.put(j1)
        store.put(j2)

        # Start queue to trigger recovery logic (without running workers).
        q = _NoopJobQueue(store, concurrency=1)
        await q.start()
        await asyncio.sleep(0.1)

        r1 = store.get(j1.id)
        r2 = store.get(j2.id)
        assert r1 is not None and r2 is not None
        assert r1.state == JobState.QUEUED, f"expected QUEUED for {j1.id}, got {r1.state}"
        assert r2.state == JobState.QUEUED, f"expected QUEUED for {j2.id}, got {r2.state}"
        assert "Recovered after restart" in str(r1.message), f"missing recovery message for {j1.id}"
        assert "Recovered after restart" in str(r2.message), f"missing recovery message for {j2.id}"

        await q.stop()


def main() -> int:
    try:
        asyncio.run(_run())
    except Exception as ex:
        print("e2e_job_recovery: FAIL")
        print(f"- error: {ex}")
        print("- hint: recovery should re-queue RUNNING/QUEUED jobs with a clear message")
        return 2
    print("e2e_job_recovery: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
