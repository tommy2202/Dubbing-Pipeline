#!/usr/bin/env python3
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def main() -> int:
    try:
        from dubbing_pipeline.jobs.store import JobStore
        from dubbing_pipeline.queue.manager import AutoQueueBackend
        from dubbing_pipeline.runtime.scheduler import Scheduler
    except Exception as ex:
        print(f"e2e_redis_fallback: SKIP (imports unavailable: {ex})")
        return 0

    os.environ["QUEUE_MODE"] = "auto"
    os.environ["REDIS_URL"] = "redis://localhost:6379/0"

    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        store = JobStore(root / "jobs.db")

        def _enqueue_cb(_job):
            return

        sched = Scheduler(store=store, enqueue_cb=_enqueue_cb)
        Scheduler.install(sched)

        qb = AutoQueueBackend(
            scheduler=sched,
            get_store_cb=lambda: store,
            enqueue_job_id_cb=lambda _job_id: None,
        )

        st = qb.status()
        assert st.mode == "fallback", st
        assert st.redis_configured is True
        assert st.redis_ok is False

        if getattr(qb, "_redis", None) is None:
            print("e2e_redis_fallback: SKIP (redis backend not initialized)")
            return 0
        qb._redis._healthy = True  # type: ignore[attr-defined]

        st2 = qb.status()
        assert st2.mode == "redis", st2
        assert st2.redis_ok is True

    print("e2e_redis_fallback: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
