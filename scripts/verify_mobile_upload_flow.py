#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _need_tool(name: str) -> bool:
    if shutil.which(name):
        return True
    print(f"verify_mobile_upload_flow: SKIP (missing {name})")
    return False


def _run(cmd: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, text=True, capture_output=True, timeout=timeout)  # nosec B603


def _sha256_hex(b: bytes) -> str:
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()


def _make_tiny_mp4(path: Path) -> None:
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


def _post_chunk_with_retry(
    c: TestClient,
    *,
    upload_id: str,
    idx: int,
    offset: int,
    chunk: bytes,
    csrf: str,
    simulate_drop: bool = False,
    max_retries: int = 3,
) -> dict:
    attempt = 0
    while True:
        attempt += 1
        res = c.post(
            f"/api/uploads/{upload_id}/chunk",
            params={"index": idx, "offset": offset},
            content=chunk,
            headers={"X-Chunk-Sha256": _sha256_hex(chunk), "X-CSRF-Token": csrf},
        )
        if simulate_drop and attempt == 1:
            # Pretend the connection dropped after the server processed the chunk.
            time.sleep(0.05)
            continue
        if res.status_code == 200:
            return res.json()
        if res.status_code in {408, 425, 429, 500, 502, 503, 504} and attempt <= max_retries:
            time.sleep(0.2 * (2**(attempt - 1)))
            continue
        raise AssertionError(f"chunk upload failed: {res.status_code} {res.text}")


def main() -> int:
    if not _need_tool("ffmpeg") or not _need_tool("ffprobe"):
        return 0

    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        inp = (root / "Input").resolve()
        out = (root / "Output").resolve()
        logs = (root / "logs").resolve()
        inp.mkdir(parents=True, exist_ok=True)
        out.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)

        mp4 = inp / "mobile_upload.mp4"
        _make_tiny_mp4(mp4)
        data = mp4.read_bytes()

        os.environ["APP_ROOT"] = str(root)
        os.environ["INPUT_DIR"] = str(inp)
        os.environ["DUBBING_OUTPUT_DIR"] = str(out)
        os.environ["DUBBING_LOG_DIR"] = str(logs)
        os.environ["COOKIE_SECURE"] = "0"
        os.environ["STRICT_SECRETS"] = "0"

        from dubbing_pipeline.api.models import AuthStore, Role, User, now_ts
        from dubbing_pipeline.api.routes_auth import router as auth_router
        from dubbing_pipeline.jobs.store import JobStore
        from dubbing_pipeline.utils.crypto import PasswordHasher
        from dubbing_pipeline.web.routes_jobs import router as jobs_router

        app = FastAPI()
        state_root = (out / "_state").resolve()
        state_root.mkdir(parents=True, exist_ok=True)
        job_store = JobStore(state_root / "jobs.db")
        app.state.job_store = job_store

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

        with TestClient(app) as c:
            csrf = _login(c, "admin", "adminpass")

            init = c.post(
                "/api/uploads/init",
                json={"filename": "mobile_upload.mp4", "total_bytes": len(data), "mime": "video/mp4"},
                headers={"X-CSRF-Token": csrf},
            )
            assert init.status_code == 200, init.text
            upload_id = init.json()["upload_id"]
            chunk_bytes = int(init.json()["chunk_bytes"])
            total_chunks = (len(data) + chunk_bytes - 1) // chunk_bytes

            off = 0
            idx = 0
            while off < len(data):
                end = min(len(data), off + chunk_bytes)
                chunk = data[off:end]
                resp = _post_chunk_with_retry(
                    c,
                    upload_id=upload_id,
                    idx=idx,
                    offset=off,
                    chunk=chunk,
                    csrf=csrf,
                    simulate_drop=(idx == 0),
                )
                assert resp.get("ok") is True

                st = c.get(f"/api/uploads/{upload_id}/status")
                assert st.status_code == 200, st.text
                stj = st.json()
                assert int(stj.get("bytes_received") or 0) >= end
                assert int(stj.get("next_expected_chunk") or 0) == min(idx + 1, total_chunks)

                off = end
                idx += 1

            done = c.post(
                f"/api/uploads/{upload_id}/complete",
                json={},
                headers={"X-CSRF-Token": csrf},
            )
            assert done.status_code == 200, done.text

            st2 = c.get(f"/api/uploads/{upload_id}/status")
            assert st2.status_code == 200, st2.text
            stj2 = st2.json()
            assert stj2.get("state") == "completed", stj2
            assert int(stj2.get("next_expected_chunk") or 0) == int(stj2.get("total_chunks") or 0)

    print("verify_mobile_upload_flow: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
