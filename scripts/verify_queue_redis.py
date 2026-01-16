from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
from contextlib import suppress
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


@dataclass
class _LocalRedis:
    proc: subprocess.Popen[str] | None = None
    port: int | None = None

    def start(self) -> str | None:
        if self.proc is not None and self.port is not None:
            return f"redis://127.0.0.1:{int(self.port)}/0"

        # Require redis-server binary.
        try:
            subprocess.run(["redis-server", "--version"], check=False, capture_output=True, text=True)  # nosec B603
        except Exception:
            return None

        # Pick a free localhost port.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = int(s.getsockname()[1])
        finally:
            s.close()

        # Start Redis in foreground, localhost-only.
        args = [
            "redis-server",
            "--save",
            "",
            "--appendonly",
            "no",
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
        ]
        self.proc = subprocess.Popen(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)  # nosec B603
        self.port = port

        # Wait briefly for readiness (best-effort).
        t0 = time.time()
        while time.time() - t0 < 3.0:
            try:
                ping = subprocess.run(  # nosec B603
                    ["redis-cli", "-h", "127.0.0.1", "-p", str(port), "PING"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=1,
                )
                if ping.returncode == 0 and "PONG" in (ping.stdout or ""):
                    break
            except Exception:
                pass
            time.sleep(0.05)

        return f"redis://127.0.0.1:{port}/0"

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()
        finally:
            self.proc = None
            self.port = None


async def _main_async() -> int:
    # Make Redis available (connect if provided; else try local docker).
    redis_url = str(os.environ.get("REDIS_URL") or "").strip()
    docker_redis = _DockerRedis()
    local_redis = _LocalRedis()
    if not redis_url:
        if os.environ.get("SKIP_DOCKER", "").strip() == "1":
            print("verify_queue_redis: SKIP (REDIS_URL unset and SKIP_DOCKER=1)")
            return 0
        try:
            docker_redis.start()
            redis_url = "redis://127.0.0.1:6379/0"
            os.environ["REDIS_URL"] = redis_url
        except Exception as ex:
            # Fall back to launching a local redis-server if available.
            try:
                url = local_redis.start()
                if not url:
                    raise RuntimeError("redis-server not available")
                redis_url = url
                os.environ["REDIS_URL"] = redis_url
            except Exception as ex2:
                print(f"verify_queue_redis: SKIP (no docker redis, no local redis): {ex} / {ex2}")
                return 0

    # Configure queue to prefer Redis. Use a unique prefix to avoid interference.
    os.environ["QUEUE_MODE"] = "redis"
    os.environ["REDIS_QUEUE_PREFIX"] = f"dp_test_{int(time.time()*1000)}"
    os.environ["REDIS_LOCK_TTL_MS"] = "60000"
    os.environ["REDIS_LOCK_REFRESH_MS"] = "5000"
    os.environ["REDIS_QUEUE_MAX_ATTEMPTS"] = "3"

    from dubbing_pipeline.queue.redis_queue import RedisQueue

    enq: list[str] = []

    async def _enqueue(job_id: str) -> None:
        enq.append(str(job_id))

    # Job state callback: pretend all are QUEUED
    def _job_state(_job_id: str) -> str:
        return "QUEUED"

    try:
        # Create queue (no background consumer loop needed for this verifier).
        q = RedisQueue(redis_url=redis_url, enqueue_job_id_cb=_enqueue, get_job_state_cb=_job_state)

        # Submit a few jobs with priorities.
        await q.submit_job(job_id="j1", user_id="u1", mode="low", device="cpu", priority=10)
        await q.submit_job(job_id="j2", user_id="u1", mode="low", device="cpu", priority=20)
        await q.submit_job(job_id="j3", user_id="u2", mode="low", device="cpu", priority=30)

        # Pending ordering should be highest priority first (j3), then j2, then j1.
        snap = await q.admin_snapshot(limit=10)
        assert snap.get("ok") is True, snap
        pending = [str(x.get("job_id") or "") for x in (snap.get("pending") or [])]
        assert pending[:3] == ["j3", "j2", "j1"], pending

        # Lock should be exclusive (do not call before_job_run twice on same instance; use a second instance).
        ok1 = await q.before_job_run(job_id="j1", user_id="u1")
        assert ok1 is True
        q_other = RedisQueue(redis_url=redis_url, enqueue_job_id_cb=_enqueue, get_job_state_cb=_job_state)
        ok1b = await q_other.before_job_run(job_id="j1", user_id="u1")
        assert ok1b is False

        # Per-user active cap (defaults to 1 in settings). j2 is same user u1.
        ok2 = await q.before_job_run(job_id="j2", user_id="u1")
        assert ok2 is False

        # Different user should be allowed.
        ok3 = await q.before_job_run(job_id="j3", user_id="u2")
        assert ok3 is True

        # Finish jobs (release locks + counters).
        await q.after_job_run(job_id="j1", user_id="u1", final_state="DONE", ok=True, error=None)
        await q.after_job_run(job_id="j3", user_id="u2", final_state="DONE", ok=True, error=None)

        print("verify_queue_redis: OK")
        return 0
    finally:
        # No background tasks are started in this verifier, but be defensive.
        with suppress(Exception):
            await q.stop()  # type: ignore[name-defined]
        docker_redis.stop()
        local_redis.stop()


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())

