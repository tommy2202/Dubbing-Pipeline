#!/usr/bin/env python3
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _login(c: TestClient, username: str, password: str) -> str:
    r = c.post("/auth/login", json={"username": username, "password": password, "session": True})
    assert r.status_code == 200, r.text
    token = r.json().get("csrf_token") or ""
    assert token
    return str(token)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        out = (root / "Output").resolve()
        logs = (root / "logs").resolve()
        out.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)

        os.environ["APP_ROOT"] = str(root)
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
        job_queue = JobQueue(job_store, concurrency=1)
        app.state.job_store = job_store
        app.state.job_queue = job_queue

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

        job_id = "job_cancel_midrun"
        job_store.put(
            Job(
                id=job_id,
                owner_id="u_admin",
                video_path="/dev/null",
                duration_s=1.0,
                mode="low",
                device="cpu",
                src_lang="auto",
                tgt_lang="en",
                created_at=now_utc(),
                updated_at=now_utc(),
                state=JobState.RUNNING,
                progress=0.3,
                message="Running",
                output_mkv="",
                output_srt="",
                work_dir="",
                log_path="",
                error=None,
            )
        )

        with TestClient(app) as c:
            csrf = _login(c, "admin", "adminpass")
            r = c.post(f"/api/jobs/{job_id}/cancel", headers={"X-CSRF-Token": csrf})
            assert r.status_code == 200, r.text
            j = job_store.get(job_id)
            assert j is not None, "job missing"
            assert j.state == JobState.CANCELED, j.state

    print("e2e_cancel_midrun: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
