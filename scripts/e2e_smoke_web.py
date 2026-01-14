#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import CancelledError as FuturesCancelledError
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _need_tool(name: str) -> None:
    if shutil.which(name):
        return
    raise RuntimeError(f"Missing required tool: {name}. Install it and retry.")


def _run(cmd: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, text=True, capture_output=True, timeout=timeout)  # nosec B603


def _sha256_hex(b: bytes) -> str:
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()


def _make_tiny_mp4(path: Path) -> None:
    _need_tool("ffmpeg")
    p = _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
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
        timeout=60,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip() or "ffmpeg failed")


def _login(c: TestClient, username: str, password: str) -> str:
    r = c.post("/auth/login", json={"username": username, "password": password, "session": True})
    assert r.status_code == 200, r.text
    token = r.json().get("csrf_token") or ""
    assert token
    return str(token)


@dataclass
class _FakeScheduler:
    def submit(self, *_args, **_kwargs) -> None:
        # For this smoke test we validate API wiring, not actual processing.
        return


def main() -> int:
    _need_tool("ffmpeg")
    _need_tool("ffprobe")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        inp = (root / "Input").resolve()
        out = (root / "Output").resolve()
        logs = (root / "logs").resolve()
        inp.mkdir(parents=True, exist_ok=True)
        out.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)

        mp4 = inp / "tiny.mp4"
        _make_tiny_mp4(mp4)
        data = mp4.read_bytes()

        # Minimal app wiring (avoid background workers/models in a smoke test).
        os.environ["APP_ROOT"] = str(root)
        os.environ["INPUT_DIR"] = str(inp)
        os.environ["DUBBING_OUTPUT_DIR"] = str(out)
        os.environ["DUBBING_LOG_DIR"] = str(logs)
        os.environ["COOKIE_SECURE"] = "0"
        os.environ["STRICT_SECRETS"] = "0"
        os.environ["REMOTE_ACCESS_MODE"] = "off"

        from dubbing_pipeline.api.models import AuthStore, Role, User, now_ts
        from dubbing_pipeline.api.routes_auth import router as auth_router
        from dubbing_pipeline.jobs.queue import JobQueue
        from dubbing_pipeline.jobs.store import JobStore
        from dubbing_pipeline.queue.fallback_local_queue import FallbackLocalQueue
        from dubbing_pipeline.utils.crypto import PasswordHasher
        from dubbing_pipeline.web.routes_jobs import router as jobs_router

        app = FastAPI()
        state_root = (out / "_state").resolve()
        state_root.mkdir(parents=True, exist_ok=True)

        job_store = JobStore(state_root / "jobs.db")
        job_queue = JobQueue(job_store, concurrency=1)
        app.state.job_store = job_store
        app.state.job_queue = job_queue

        auth_store = AuthStore(state_root / "auth.db")
        app.state.auth_store = auth_store

        app.state.scheduler = _FakeScheduler()
        app.state.queue_backend = FallbackLocalQueue(get_store_cb=lambda: job_store, scheduler=app.state.scheduler)

        ph = PasswordHasher()
        auth_store.upsert_user(
            User(
                id="u_admin",
                username="admin",
                password_hash=ph.hash("adminpass"),
                role=Role.admin,
                totp_secret=None,
                totp_enabled=False,
                created_at=now_ts(),
            )
        )

        app.include_router(auth_router)
        app.include_router(jobs_router)

        try:
            with TestClient(app) as c:
                csrf = _login(c, "admin", "adminpass")

                init = c.post(
                    "/api/uploads/init",
                    json={"filename": "tiny.mp4", "total_bytes": len(data), "mime": "video/mp4"},
                    headers={"X-CSRF-Token": csrf},
                )
                assert init.status_code == 200, init.text
                upload_id = init.json()["upload_id"]
                chunk_bytes = int(init.json()["chunk_bytes"])

                off = 0
                idx = 0
                while off < len(data):
                    end = min(len(data), off + chunk_bytes)
                    chunk = data[off:end]
                    rr = c.post(
                        f"/api/uploads/{upload_id}/chunk",
                        params={"index": idx, "offset": off},
                        content=chunk,
                        headers={"X-Chunk-Sha256": _sha256_hex(chunk), "X-CSRF-Token": csrf},
                    )
                    assert rr.status_code == 200, rr.text
                    off = end
                    idx += 1

                done = c.post(
                    f"/api/uploads/{upload_id}/complete",
                    json={"final_sha256": _sha256_hex(data)},
                    headers={"X-CSRF-Token": csrf},
                )
                assert done.status_code == 200, done.text

                # Submit a job (validate end-to-end creation). Use a fake scheduler so nothing runs.
                jobr = c.post(
                    "/api/jobs",
                    json={
                        "upload_id": upload_id,
                        "series_title": "Reliability Test Series",
                        "season_number": 1,
                        "episode_number": 1,
                        "mode": "low",
                        "device": "cpu",
                        "src_lang": "auto",
                        "tgt_lang": "en",
                        "pg": "off",
                        "qa": False,
                        "cache_policy": "full",
                    },
                    headers={"X-CSRF-Token": csrf},
                )
                assert jobr.status_code == 200, jobr.text
                job_id = jobr.json()["id"]

                cc = c.post(f"/api/jobs/{job_id}/cancel", headers={"X-CSRF-Token": csrf})
                assert cc.status_code == 200, cc.text
        except FuturesCancelledError:
            # Some Starlette/AnyIO combos can raise CancelledError during shutdown.
            pass

    print("e2e_smoke_web: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

