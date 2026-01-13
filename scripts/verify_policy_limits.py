from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except Exception as ex:  # pragma: no cover
    print(f"verify_policy_limits: SKIP (fastapi not installed): {ex}")
    raise SystemExit(0)


@dataclass
class _DockerRedis:
    cid: str | None = None

    def start(self) -> str | None:
        if self.cid:
            return "redis://127.0.0.1:6379/0"
        try:
            cid = (
                subprocess.check_output(  # nosec B603
                    ["docker", "run", "-d", "--rm", "-p", "127.0.0.1:6379:6379", "redis:7-alpine"],
                    text=True,
                )
                .strip()
                .splitlines()[0]
            )
            self.cid = cid
            return "redis://127.0.0.1:6379/0"
        except Exception:
            return None

    def stop(self) -> None:
        if not self.cid:
            return
        subprocess.run(["docker", "stop", self.cid], check=False)  # nosec B603
        self.cid = None


def _make_dummy_mp4(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # nosec B603
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=10",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t",
            "1.0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    docker_redis = _DockerRedis()
    try:
        redis_url = os.environ.get("REDIS_URL") or docker_redis.start()
        if not redis_url:
            print("verify_policy_limits: SKIP (no redis available)")
            return 0
        os.environ["REDIS_URL"] = redis_url
        os.environ["QUEUE_MODE"] = "redis"
        os.environ["REDIS_QUEUE_PREFIX"] = "dp_verify_policy"
        os.environ["REMOTE_ACCESS_MODE"] = "off"

        # Safe defaults
        os.environ["DUBBING_MAX_ACTIVE_JOBS_PER_USER"] = "1"
        os.environ["DUBBING_MAX_QUEUED_JOBS_PER_USER"] = "5"
        os.environ["DUBBING_HIGH_MODE_ADMIN_ONLY"] = "1"
        os.environ["MAX_HIGH_RUNNING_GLOBAL"] = "1"

        from dubbing_pipeline.api.models import AuthStore, Role, User, now_ts
        from dubbing_pipeline.api.routes_admin import router as admin_router
        from dubbing_pipeline.api.routes_auth import router as auth_router
        from dubbing_pipeline.jobs.queue import JobQueue
        from dubbing_pipeline.jobs.store import JobStore
        from dubbing_pipeline.queue.manager import AutoQueueBackend
        from dubbing_pipeline.runtime.scheduler import Scheduler
        from dubbing_pipeline.utils.crypto import PasswordHasher
        from dubbing_pipeline.web.routes_jobs import router as jobs_router

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            os.environ["APP_ROOT"] = str(root)
            os.environ["DUBBING_OUTPUT_DIR"] = str((root / "Output").resolve())
            os.environ["DUBBING_LOG_DIR"] = str((root / "logs").resolve())

            in_dir = root / "Input"
            in_dir.mkdir(parents=True, exist_ok=True)
            src_mp4 = in_dir / "tiny.mp4"
            _make_dummy_mp4(src_mp4)

            app = FastAPI()
            out_root = Path(os.environ["DUBBING_OUTPUT_DIR"]).resolve()
            out_root.mkdir(parents=True, exist_ok=True)
            state_root = (out_root / "_state").resolve()
            state_root.mkdir(parents=True, exist_ok=True)

            store = JobStore(state_root / "jobs.db")
            app.state.job_store = store
            q = JobQueue(store, concurrency=1)
            app.state.job_queue = q

            # scheduler enqueue callback isn't used in this verification; jobs can remain queued.
            def _enqueue_cb(_job):  # noqa: ANN001
                return

            sched = Scheduler(store=store, enqueue_cb=_enqueue_cb)
            Scheduler.install(sched)
            sched.start()
            app.state.scheduler = sched

            qb = AutoQueueBackend(
                scheduler=sched,
                get_store_cb=lambda: app.state.job_store,
                enqueue_job_id_cb=lambda job_id: q.enqueue_id(job_id),
            )
            # Start queue backend (redis).
            import asyncio as _asyncio

            _asyncio.get_event_loop().run_until_complete(qb.start())
            app.state.queue_backend = qb
            q.queue_backend = qb

            auth_store = AuthStore(state_root / "auth.db")
            app.state.auth_store = auth_store
            ph = PasswordHasher()
            admin = User(
                id="u_admin",
                username="admin",
                password_hash=ph.hash("adminpass"),
                role=Role.admin,
                totp_secret=None,
                totp_enabled=False,
                created_at=now_ts(),
            )
            alice = User(
                id="u_alice",
                username="alice",
                password_hash=ph.hash("password123"),
                role=Role.operator,
                totp_secret=None,
                totp_enabled=False,
                created_at=now_ts(),
            )
            auth_store.upsert_user(admin)
            auth_store.upsert_user(alice)

            app.include_router(auth_router)
            app.include_router(jobs_router)
            app.include_router(admin_router)

            with TestClient(app) as c:
                # Admin login to set quotas
                r0 = c.post(
                    "/auth/login", json={"username": "admin", "password": "adminpass", "session": True}
                )
                assert r0.status_code == 200, r0.text
                csrf = c.cookies.get("csrf") or ""
                assert csrf
                rr = c.post(
                    "/api/admin/users/u_alice/quotas",
                    json={"max_running": 1, "max_queued": 1},
                    headers={"X-CSRF-Token": csrf},
                )
                assert rr.status_code == 200, rr.text

                # Switch to alice session
                c.post("/auth/logout", headers={"X-CSRF-Token": csrf})
                r1 = c.post(
                    "/auth/login",
                    json={"username": "alice", "password": "password123", "session": True},
                )
                assert r1.status_code == 200, r1.text
                csrf2 = c.cookies.get("csrf") or ""
                assert csrf2

                # Chunked upload
                data = src_mp4.read_bytes()
                init = c.post(
                    "/api/uploads/init",
                    json={"filename": "tiny.mp4", "total_bytes": len(data), "mime": "video/mp4"},
                    headers={"X-CSRF-Token": csrf2},
                )
                assert init.status_code == 200, init.text
                upload_id = init.json()["upload_id"]
                chunk_bytes = int(init.json().get("chunk_bytes") or 262144)

                off = 0
                idx = 0
                while off < len(data):
                    end = min(len(data), off + chunk_bytes)
                    chunk = data[off:end]
                    rr2 = c.post(
                        f"/api/uploads/{upload_id}/chunk?index={idx}&offset={off}",
                        content=chunk,
                        headers={
                            "content-type": "application/octet-stream",
                            "X-Chunk-Sha256": _sha256_hex(chunk),
                            "X-CSRF-Token": csrf2,
                        },
                    )
                    assert rr2.status_code == 200, rr2.text
                    off = end
                    idx += 1
                done = c.post(
                    f"/api/uploads/{upload_id}/complete", json={}, headers={"X-CSRF-Token": csrf2}
                )
                assert done.status_code == 200, done.text

                # First job ok (queued)
                j1 = c.post(
                    "/api/jobs",
                    json={
                        "upload_id": upload_id,
                        "mode": "low",
                        "device": "cpu",
                        "src_lang": "auto",
                        "tgt_lang": "en",
                        "series_title": "My Show",
                        "season_text": "1",
                        "episode_text": "1",
                    },
                    headers={"X-CSRF-Token": csrf2},
                )
                assert j1.status_code == 200, j1.text

                # Second job should be rejected by quota (max_queued=1) while first is queued.
                j2 = c.post(
                    "/api/jobs",
                    json={
                        "upload_id": upload_id,
                        "mode": "low",
                        "device": "cpu",
                        "src_lang": "auto",
                        "tgt_lang": "en",
                        "series_title": "My Show",
                        "season_text": "1",
                        "episode_text": "2",
                    },
                    headers={"X-CSRF-Token": csrf2},
                )
                assert j2.status_code == 429, j2.text

                # High mode admin-only: operator should be rejected.
                jh = c.post(
                    "/api/jobs",
                    json={
                        "upload_id": upload_id,
                        "mode": "high",
                        "device": "cpu",
                        "src_lang": "auto",
                        "tgt_lang": "en",
                        "series_title": "My Show",
                        "season_text": "1",
                        "episode_text": "3",
                    },
                    headers={"X-CSRF-Token": csrf2},
                )
                assert jh.status_code == 403, jh.text

            print("verify_policy_limits: OK")
            return 0
    finally:
        docker_redis.stop()


if __name__ == "__main__":
    raise SystemExit(main())