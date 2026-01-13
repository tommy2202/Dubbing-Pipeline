from __future__ import annotations

import asyncio
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class _FakeScheduler:
    submitted: list[str]

    def submit(self, rec) -> None:  # noqa: ANN001
        self.submitted.append(str(getattr(rec, "job_id", "")))


async def _main_async() -> int:
    # Force fallback mode and ensure Redis is "down".
    os.environ.pop("REDIS_URL", None)
    os.environ["QUEUE_MODE"] = "fallback"

    from anime_v2.jobs.store import JobStore
    from anime_v2.queue.fallback_local_queue import FallbackLocalQueue
    from anime_v2.runtime.scheduler import JobRecord, Scheduler

    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        store = JobStore(root / "jobs.db")

        # We can use the real Scheduler for type shape, but do not start its thread here.
        # Instead, use a minimal fake scheduler interface.
        fake = _FakeScheduler(submitted=[])

        # Shim Scheduler-like object with submit()
        sched = fake  # type: ignore[assignment]

        q = FallbackLocalQueue(get_store_cb=lambda: store, scheduler=sched)  # type: ignore[arg-type]
        await q.start()
        try:
            await q.submit_job(job_id="j_fallback_1", user_id="u1", mode="low", device="cpu", priority=100)
            assert fake.submitted == ["j_fallback_1"], fake.submitted

            # before/after hooks are no-ops but should not error
            ok = await q.before_job_run(job_id="j_fallback_1", user_id="u1")
            assert ok is True
            await q.after_job_run(job_id="j_fallback_1", user_id="u1", final_state="DONE", ok=True)

            st = q.status()
            assert st.mode == "fallback"
            print("verify_queue_fallback: OK")
            return 0
        finally:
            await q.stop()


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())

