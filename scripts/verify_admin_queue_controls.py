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
    print(f"verify_admin_queue_controls: SKIP (fastapi not installed): {ex}")
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
            print("verify_admin_queue_controls: SKIP (no redis available)")
            return 0
        os.environ["REDIS_URL"] = redis_url
        os.environ["QUEUE_MODE"] = "redis"
        os.environ["REDIS_QUEUE_PREFIX"] = "dp_verify_admin"
        os.environ["REMOTE_ACCESS_MODE"] = "off"

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

            def _enqueue_cb(_job):  # noqa: ANN001
                return

            sched = Scheduler(store=store, enqueue_cb=_enqueue_cb)
            Scheduler.install(sched)
            sched.start()
            app.state.scheduler = sched

            import asyncio as _asyncio

            qb = AutoQueueBackend(
                scheduler=sched,
                get_store_cb=lambda: app.state.job_store,
                enqueue_job_id_cb=lambda job_id: q.enqueue_id(job_id),
            )
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
                # login as alice to create a job (queued)
                r1 = c.post(
                    "/auth/login",
                    json={"username": "alice", "password": "password123", "session": True},
                )
                assert r1.status_code == 200, r1.text
                csrf = c.cookies.get("csrf") or ""
                assert csrf

                data = src_mp4.read_bytes()
                init = c.post(
                    "/api/uploads/init",
                    json={"filename": "tiny.mp4", "total_bytes": len(data), "mime": "video/mp4"},
                    headers={"X-CSRF-Token": csrf},
                )
                assert init.status_code == 200, init.text
                upload_id = init.json()["upload_id"]
                chunk_bytes = int(init.json().get("chunk_bytes") or 262144)
                off = 0
                idx = 0
                while off < len(data):
                    end = min(len(data), off + chunk_bytes)
                    chunk = data[off:end]
                    rr = c.post(
                        f"/api/uploads/{upload_id}/chunk?index={idx}&offset={off}",
                        content=chunk,
                        headers={
                            "content-type": "application/octet-stream",
                            "X-Chunk-Sha256": _sha256_hex(chunk),
                            "X-CSRF-Token": csrf,
                        },
                    )
                    assert rr.status_code == 200, rr.text
                    off = end
                    idx += 1
                done = c.post(
                    f"/api/uploads/{upload_id}/complete", json={}, headers={"X-CSRF-Token": csrf}
                )
                assert done.status_code == 200, done.text

                j = c.post(
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
                    headers={"X-CSRF-Token": csrf},
                )
                assert j.status_code == 200, j.text
                job_id = j.json()["id"]

                # login as admin for admin controls
                c.post("/auth/logout", headers={"X-CSRF-Token": csrf})
                r2 = c.post(
                    "/auth/login", json={"username": "admin", "password": "adminpass", "session": True}
                )
                assert r2.status_code == 200, r2.text
                csrf2 = c.cookies.get("csrf") or ""

                # queue snapshot includes the job in pending
                snap = c.get("/api/admin/queue?limit=200", headers={"X-CSRF-Token": csrf2})
                assert snap.status_code == 200, snap.text
                data2 = snap.json().get("backend") or {}
                pending = data2.get("pending") or []
                assert any(str(it.get("job_id")) == job_id for it in pending), pending

                # reprioritize
                pr = c.post(
                    f"/api/admin/jobs/{job_id}/priority",
                    json={"priority": 900},
                    headers={"X-CSRF-Token": csrf2},
                )
                assert pr.status_code == 200, pr.text

                # cancel
                cc = c.post(
                    f"/api/admin/jobs/{job_id}/cancel", json={}, headers={"X-CSRF-Token": csrf2}
                )
                assert cc.status_code == 200, cc.text

            print("verify_admin_queue_controls: OK")
            return 0
    finally:
        docker_redis.stop()


if __name__ == "__main__":
    raise SystemExit(main())

