from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from uuid import uuid4

try:
    from fastapi.testclient import TestClient
except Exception as ex:  # pragma: no cover - optional deps
    print(f"verify_scale_path: SKIP (fastapi unavailable: {ex})")
    raise SystemExit(0)

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.queue.redis_queue import RedisQueue
from dubbing_pipeline.server import app


def _local_check() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        in_dir = root / "Input"
        out_dir = root / "Output"
        logs_dir = root / "logs"
        in_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)

        os.environ["APP_ROOT"] = str(root)
        os.environ["INPUT_DIR"] = str(in_dir)
        os.environ["DUBBING_OUTPUT_DIR"] = str(out_dir)
        os.environ["DUBBING_LOG_DIR"] = str(logs_dir)
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "adminpass"
        os.environ["COOKIE_SECURE"] = "0"
        os.environ["QUEUE_BACKEND"] = "local"
        os.environ["STORE_BACKEND"] = "local"
        os.environ["REDIS_URL"] = ""
        get_settings.cache_clear()

        with TestClient(app) as c:
            qb = c.app.state.queue_backend
            st = qb.status()
            assert st.mode == "fallback", f"expected local fallback, got {st.mode}"


async def _redis_check(redis_url: str) -> None:
    # Use a unique prefix to avoid touching real queues.
    os.environ["REDIS_QUEUE_PREFIX"] = f"dp_verify_{uuid4().hex[:8]}"
    get_settings.cache_clear()
    job_id = f"job_verify_{uuid4().hex[:6]}"
    q = RedisQueue(redis_url=redis_url, enqueue_job_id_cb=lambda _job_id: None)
    await q.submit_job(
        job_id=job_id,
        user_id="u_verify",
        mode="low",
        device="cpu",
        priority=50,
        meta={"user_role": "operator"},
    )
    snap = await q.admin_snapshot(limit=5)
    assert bool(snap.get("ok")), "redis admin_snapshot failed"
    pending = snap.get("pending") if isinstance(snap, dict) else []
    assert any(p.get("job_id") == job_id for p in (pending or [])), "job not queued"
    await q.cancel_job(job_id=job_id, user_id="u_verify")


def main() -> int:
    _local_check()

    redis_url = str(os.environ.get("REDIS_URL") or "").strip()
    if not redis_url:
        print("verify_scale_path: OK (redis SKIP: REDIS_URL not set)")
        return 0
    try:
        import redis.asyncio as _redis  # type: ignore  # noqa: F401
    except Exception as ex:
        print(f"verify_scale_path: OK (redis SKIP: {ex})")
        return 0

    try:
        asyncio.run(_redis_check(redis_url))
    except Exception as ex:
        raise SystemExit(f"verify_scale_path: redis check failed: {ex}") from ex

    print("verify_scale_path: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
