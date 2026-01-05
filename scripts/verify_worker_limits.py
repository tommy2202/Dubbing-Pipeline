from __future__ import annotations

import threading
import time
from pathlib import Path


def _sleep(seconds: float) -> None:
    time.sleep(float(seconds))


def main() -> int:
    from anime_v2.jobs.models import Job, JobState
    from anime_v2.jobs.store import JobStore
    from anime_v2.jobs.watchdog import PhaseTimeout, run_with_timeout

    # 1) Timeout triggers
    t0 = time.perf_counter()
    try:
        run_with_timeout("sleep", timeout_s=1, fn=_sleep, args=(10.0,))
        raise AssertionError("expected PhaseTimeout")
    except PhaseTimeout:
        dt = time.perf_counter() - t0
        assert dt < 5.0

    # 2) Cancel triggers early termination
    out = Path("/tmp") / "anime_v2_worker_limits"
    out.mkdir(parents=True, exist_ok=True)
    store = JobStore(out / "jobs.db")
    job = Job(
        id="j_limits_1",
        owner_id="u1",
        video_path="/tmp/none.mp4",
        duration_s=60.0,
        request_id="",
        mode="low",
        device="cpu",
        src_lang="ja",
        tgt_lang="en",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        state=JobState.RUNNING,
        progress=0.0,
        message="Running",
        output_mkv="",
        output_srt="",
        work_dir=str(out),
        log_path=str(out / "job.log"),
        error=None,
    )
    store.put(job)

    def _cancel_later():
        time.sleep(0.5)
        store.update(job.id, state=JobState.CANCELED, message="Canceled")

    th = threading.Thread(target=_cancel_later, daemon=True)
    th.start()

    def _cancel_check() -> bool:
        j = store.get(job.id)
        return j is not None and j.state == JobState.CANCELED

    t1 = time.perf_counter()
    try:
        run_with_timeout(
            "sleep_cancelable",
            timeout_s=30,
            fn=_sleep,
            args=(30.0,),
            cancel_check=_cancel_check,
            cancel_exc=RuntimeError("canceled"),
        )
        raise AssertionError("expected cancel exception")
    except RuntimeError as ex:
        assert "canceled" in str(ex)
        dt = time.perf_counter() - t1
        assert dt < 10.0

    print("verify_worker_limits: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

