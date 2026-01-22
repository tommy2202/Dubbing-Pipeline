#!/usr/bin/env python3
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


def _redis_ping(url: str) -> bool:
    try:
        import redis  # type: ignore

        r = redis.Redis.from_url(url, decode_responses=True)
        return bool(r.ping())
    except Exception:
        return False


def _wait_for_status(qb, *, want_mode: str, timeout_s: float = 4.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        st = qb.status()
        if str(getattr(st, "mode", "")).lower() == want_mode:
            return True
        time.sleep(0.1)
    return False


def main() -> int:
    # Phase 1: Redis down -> fallback
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        os.environ["APP_ROOT"] = str(root)
        os.environ["DUBBING_OUTPUT_DIR"] = str(root / "Output")
        os.environ["DUBBING_LOG_DIR"] = str(root / "logs")
        os.environ["DUBBING_STATE_DIR"] = str(root / "_state")
        os.environ["QUEUE_MODE"] = "auto"
        os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/0"

        from dubbing_pipeline.jobs.store import JobStore
        from dubbing_pipeline.runtime.scheduler import Scheduler
        from dubbing_pipeline.queue.manager import AutoQueueBackend

        store = JobStore(root / "_state" / "jobs.db")
        sched = Scheduler(store=store, enqueue_cb=lambda _j: None)
        Scheduler.install(sched)
        sched.start()

        qb = AutoQueueBackend(
            scheduler=sched,
            get_store_cb=lambda: store,
            enqueue_job_id_cb=lambda _job_id: None,
        )
        import asyncio

        async def _run():
            await qb.start()
            ok = _wait_for_status(qb, want_mode="fallback")
            await qb.stop()
            return ok

        ok = asyncio.run(_run())
        if not ok:
            print("e2e_redis_fallback: FAIL (fallback not active)")
            return 2

    # Phase 2: Redis back (optional)
    real_url = os.environ.get("REDIS_URL_REAL", "").strip() or "redis://127.0.0.1:6379/0"
    if not _redis_ping(real_url):
        print("e2e_redis_fallback: SKIP (redis not reachable for 'back' check)")
        return 0

    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        os.environ["APP_ROOT"] = str(root)
        os.environ["DUBBING_OUTPUT_DIR"] = str(root / "Output")
        os.environ["DUBBING_LOG_DIR"] = str(root / "logs")
        os.environ["DUBBING_STATE_DIR"] = str(root / "_state")
        os.environ["QUEUE_MODE"] = "auto"
        os.environ["REDIS_URL"] = real_url

        from dubbing_pipeline.jobs.store import JobStore
        from dubbing_pipeline.runtime.scheduler import Scheduler
        from dubbing_pipeline.queue.manager import AutoQueueBackend

        store = JobStore(root / "_state" / "jobs.db")
        sched = Scheduler(store=store, enqueue_cb=lambda _j: None)
        Scheduler.install(sched)
        sched.start()

        qb = AutoQueueBackend(
            scheduler=sched,
            get_store_cb=lambda: store,
            enqueue_job_id_cb=lambda _job_id: None,
        )
        import asyncio

        async def _run():
            await qb.start()
            ok = _wait_for_status(qb, want_mode="redis")
            await qb.stop()
            return ok

        ok = asyncio.run(_run())
        if not ok:
            print("e2e_redis_fallback: FAIL (redis not active when reachable)")
            return 2

    print("e2e_redis_fallback: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
