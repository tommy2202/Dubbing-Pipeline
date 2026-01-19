#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def _need_tool(name: str) -> None:
    if shutil.which(name):
        return
    raise RuntimeError(f"Missing required tool: {name}. Install it and retry.")


def _run(cmd: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, text=True, capture_output=True, timeout=timeout)  # nosec B603


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


def _login(c, username: str, password: str) -> str:
    r = c.post("/auth/login", json={"username": username, "password": password, "session": True})
    assert r.status_code == 200, r.text
    token = r.json().get("csrf_token") or ""
    assert token
    return str(token)


def main() -> int:
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception as ex:
        print(f"e2e_cancel_midrun: SKIP (fastapi unavailable: {ex})")
        return 0
    try:
        _need_tool("ffmpeg")
    except RuntimeError as ex:
        print(f"e2e_cancel_midrun: SKIP ({ex})")
        return 0

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

        os.environ["APP_ROOT"] = str(root)
        os.environ["INPUT_DIR"] = str(inp)
        os.environ["DUBBING_OUTPUT_DIR"] = str(out)
        os.environ["DUBBING_LOG_DIR"] = str(logs)
        os.environ["COOKIE_SECURE"] = "0"
        os.environ["STRICT_SECRETS"] = "0"

        from dubbing_pipeline.api.models import AuthStore, Role, User, now_ts
        from dubbing_pipeline.api.routes_auth import router as auth_router
        from dubbing_pipeline.jobs.models import Job, JobState, now_utc
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
                id="u1",
                username="user1",
                password_hash=ph.hash("pass1"),
                role=Role.operator,
                totp_secret=None,
                totp_enabled=False,
                created_at=now_ts(),
            )
        )

        job_id = "job_cancel_midrun"
        job_store.put(
            Job(
                id=job_id,
                owner_id="u1",
                video_path=str(mp4),
                duration_s=1.0,
                mode="low",
                device="cpu",
                src_lang="auto",
                tgt_lang="en",
                created_at=now_utc(),
                updated_at=now_utc(),
                state=JobState.RUNNING,
                progress=0.4,
                message="Running",
                output_mkv="",
                output_srt="",
                work_dir=str(out),
                log_path=str(out / "job.log"),
                error=None,
            )
        )

        app.include_router(auth_router)
        app.include_router(jobs_router)

        with TestClient(app) as c:
            csrf = _login(c, "user1", "pass1")
            r = c.post(f"/api/jobs/{job_id}/cancel", headers={"X-CSRF-Token": csrf})
            assert r.status_code == 200, r.text
            data = r.json()
            assert data.get("state") == "CANCELED", data
            j = job_store.get(job_id)
            assert j is not None and j.state == JobState.CANCELED

    print("e2e_cancel_midrun: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
