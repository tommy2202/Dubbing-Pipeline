#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import CancelledError as FuturesCancelledError
from pathlib import Path

from fastapi.testclient import TestClient


def _need_tool(name: str) -> bool:
    if shutil.which(name):
        return True
    print(f"e2e_upload_resume: SKIP (missing {name})")
    return False


def _run(cmd: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, text=True, capture_output=True, timeout=timeout)  # nosec B603


def _sha256_hex(b: bytes) -> str:
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()


def _make_tiny_mp4(path: Path) -> None:
    if not _need_tool("ffmpeg"):
        raise RuntimeError("ffmpeg missing")
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
            "1.2",
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


def _post_chunk(
    c: TestClient,
    *,
    upload_id: str,
    idx: int,
    off: int,
    chunk: bytes,
    csrf: str,
    simulate_loss: bool,
) -> None:
    # Simulate a flaky network: sometimes "drop" by sleeping and retrying.
    tries = 0
    while True:
        tries += 1
        if simulate_loss and random.random() < 0.15 and tries < 3:
            time.sleep(0.05)
            continue
        r = c.post(
            f"/api/uploads/{upload_id}/chunk",
            params={"index": idx, "offset": off},
            content=chunk,
            headers={"X-Chunk-Sha256": _sha256_hex(chunk), "X-CSRF-Token": csrf},
        )
        assert r.status_code == 200, r.text
        return


def main() -> int:
    random.seed(0)
    if not _need_tool("ffmpeg"):
        return 0

    simulate_loss = os.environ.get("SIMULATE_NETWORK_LOSS", "").strip() in {"1", "true", "yes"}

    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        inp = (root / "Input").resolve()
        out = (root / "Output").resolve()
        logs = (root / "logs").resolve()
        inp.mkdir(parents=True, exist_ok=True)
        out.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)

        mp4 = inp / "resume.mp4"
        _make_tiny_mp4(mp4)
        data = mp4.read_bytes()

        os.environ["APP_ROOT"] = str(root)
        os.environ["INPUT_DIR"] = str(inp)
        os.environ["DUBBING_OUTPUT_DIR"] = str(out)
        os.environ["DUBBING_LOG_DIR"] = str(logs)
        os.environ["COOKIE_SECURE"] = "0"
        os.environ["STRICT_SECRETS"] = "0"
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "adminpass"

        # Minimal app wiring (avoid background workers/models).
        from fastapi import FastAPI

        from dubbing_pipeline.api.models import AuthStore, Role, User, now_ts
        from dubbing_pipeline.api.routes_auth import router as auth_router
        from dubbing_pipeline.jobs.queue import JobQueue
        from dubbing_pipeline.jobs.store import JobStore
        from dubbing_pipeline.utils.crypto import PasswordHasher
        from dubbing_pipeline.web.routes_jobs import router as jobs_router

        app = FastAPI()
        state_root = (out / "_state").resolve()
        state_root.mkdir(parents=True, exist_ok=True)
        job_store = JobStore(state_root / "jobs.db")
        app.state.job_store = job_store
        app.state.job_queue = JobQueue(job_store, concurrency=1)
        auth_store = AuthStore(state_root / "auth.db")
        app.state.auth_store = auth_store

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
            # Client 1: init + first chunk
            with TestClient(app) as c1:
                csrf1 = _login(c1, "admin", "adminpass")
                init = c1.post(
                    "/api/uploads/init",
                    json={"filename": "resume.mp4", "total_bytes": len(data), "mime": "video/mp4"},
                    headers={"X-CSRF-Token": csrf1},
                )
                assert init.status_code == 200, init.text
                upload_id = init.json()["upload_id"]
                chunk_bytes = int(init.json()["chunk_bytes"])

                first = data[: min(len(data), chunk_bytes)]
                _post_chunk(
                    c1,
                    upload_id=upload_id,
                    idx=0,
                    off=0,
                    chunk=first,
                    csrf=csrf1,
                    simulate_loss=simulate_loss,
                )

                st = c1.get(f"/api/uploads/{upload_id}")
                assert st.status_code == 200, st.text
                rb = int(st.json().get("received_bytes") or 0)
                assert rb >= len(first), st.text

            # Client 2 (resume): re-send chunk 0 (idempotent), send remaining chunks, finalize
            with TestClient(app) as c2:
                csrf2 = _login(c2, "admin", "adminpass")

                # Re-send chunk 0 (should be idempotent)
                _post_chunk(
                    c2,
                    upload_id=upload_id,
                    idx=0,
                    off=0,
                    chunk=first,
                    csrf=csrf2,
                    simulate_loss=simulate_loss,
                )
                st2 = c2.get(f"/api/uploads/{upload_id}")
                assert st2.status_code == 200, st2.text
                rb2 = int(st2.json().get("received_bytes") or 0)
                assert rb2 >= len(first), st2.text

                off = len(first)
                idx = 1
                while off < len(data):
                    end = min(len(data), off + chunk_bytes)
                    chunk = data[off:end]
                    _post_chunk(
                        c2,
                        upload_id=upload_id,
                        idx=idx,
                        off=off,
                        chunk=chunk,
                        csrf=csrf2,
                        simulate_loss=simulate_loss,
                    )
                    off = end
                    idx += 1

                done = c2.post(
                    f"/api/uploads/{upload_id}/complete",
                    json={"final_sha256": _sha256_hex(data)},
                    headers={"X-CSRF-Token": csrf2},
                )
                assert done.status_code == 200, done.text
                vp = Path(done.json()["video_path"]).resolve()
                assert vp.exists(), f"final video_path missing: {vp}"
        except FuturesCancelledError:
            # Some Starlette/AnyIO combos can raise CancelledError during shutdown.
            pass

    print("e2e_upload_resume: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

