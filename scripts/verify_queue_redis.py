from __future__ import annotations

import asyncio
import os
import subprocess
import time
from dataclasses import dataclass


@dataclass
class _DockerRedis:
    container_id: str | None = None

    def start(self) -> None:
        if self.container_id:
            return
        # Localhost-only port binding (not internet-exposed).
        cid = (
            subprocess.check_output(  # nosec B603
                [
                    "docker",
                    "run",
                    "-d",
                    "--rm",
                    "-p",
                    "127.0.0.1:6379:6379",
                    "redis:7-alpine",
                ],
                text=True,
            )
            .strip()
            .splitlines()[0]
        )
        self.container_id = cid

    def stop(self) -> None:
        if not self.container_id:
            return
        try:
            subprocess.run(["docker", "stop", self.container_id], check=False)  # nosec B603
        finally:
            self.container_id = None


async def _main_async() -> int:
    # Make Redis available (connect if provided; else try local docker).
    redis_url = str(os.environ.get("REDIS_URL") or "").strip()
    docker_redis = _DockerRedis()
    if not redis_url:
        if os.environ.get("SKIP_DOCKER", "").strip() == "1":
            print("verify_queue_redis: SKIP (REDIS_URL unset and SKIP_DOCKER=1)")
            return 0
        try:
            docker_redis.start()
            redis_url = "redis://127.0.0.1:6379/0"
            os.environ["REDIS_URL"] = redis_url
        except Exception as ex:
            print(f"verify_queue_redis: SKIP (cannot start docker redis): {ex}")
            return 0

    # Configure queue to prefer Redis.
    os.environ["QUEUE_MODE"] = "redis"
    os.environ["REDIS_QUEUE_PREFIX"] = "dp_test"
    os.environ["REDIS_LOCK_TTL_MS"] = "60000"
    os.environ["REDIS_LOCK_REFRESH_MS"] = "5000"
    os.environ["REDIS_QUEUE_MAX_ATTEMPTS"] = "3"

    from anime_v2.queue.redis_queue import RedisQueue

    enq: list[str] = []

    async def _enqueue(job_id: str) -> None:
        enq.append(str(job_id))

    # Job state callback: pretend all are QUEUED
    def _job_state(_job_id: str) -> str:
        return "QUEUED"

    q = RedisQueue(redis_url=redis_url, enqueue_job_id_cb=_enqueue, get_job_state_cb=_job_state)
    await q.start()

    try:
        # Submit a few jobs with priorities.
        await q.submit_job(job_id="j1", user_id="u1", mode="low", device="cpu", priority=10)
        await q.submit_job(job_id="j2", user_id="u1", mode="low", device="cpu", priority=20)
        await q.submit_job(job_id="j3", user_id="u2", mode="low", device="cpu", priority=30)

        t0 = time.time()
        while time.time() - t0 < 5.0 and len(enq) < 3:
            await asyncio.sleep(0.05)

        # Highest priority should be claimed first (j3), then j2, then j1.
        assert enq[:3] == ["j3", "j2", "j1"], enq

        # Lock should be exclusive.
        ok1 = await q.before_job_run(job_id="j1", user_id="u1")
        assert ok1 is True
        ok1b = await q.before_job_run(job_id="j1", user_id="u1")
        assert ok1b is False

        # Per-user active cap (defaults to 1 in settings; this test expects it).
        # j2 is same user u1; starting it should be deferred until j1 finishes.
        ok2 = await q.before_job_run(job_id="j2", user_id="u1")
        assert ok2 is False

        # Different user should be allowed.
        ok3 = await q.before_job_run(job_id="j3", user_id="u2")
        assert ok3 is True

        # Finish jobs (release locks + ack).
        await q.after_job_run(job_id="j1", user_id="u1", final_state="DONE", ok=True, error=None)
        await q.after_job_run(job_id="j3", user_id="u2", final_state="DONE", ok=True, error=None)

        print("verify_queue_redis: OK")
        return 0
    finally:
        await q.stop()
        docker_redis.stop()


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())

