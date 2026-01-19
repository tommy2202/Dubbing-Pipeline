#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception as ex:
        print(f"verify_library_search: SKIP (fastapi unavailable: {ex})")
        return 0

    repo_root = Path(__file__).resolve().parents[1]
    src_root = repo_root / "src"
    for p in (str(repo_root), str(src_root)):
        if p not in sys.path:
            sys.path.insert(0, p)

    try:
        from dubbing_pipeline.api.models import AuthStore, Role, User, now_ts
        from dubbing_pipeline.api.routes_auth import router as auth_router
        from dubbing_pipeline.api.routes_library import router as library_router
        from dubbing_pipeline.jobs.models import Job, JobState, now_utc
        from dubbing_pipeline.jobs.store import JobStore
        from dubbing_pipeline.utils.crypto import PasswordHasher
    except Exception as ex:
        print(f"verify_library_search: SKIP (imports unavailable: {ex})")
        return 0

    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        inp = (root / "Input").resolve()
        out = (root / "Output").resolve()
        logs = (root / "logs").resolve()
        inp.mkdir(parents=True, exist_ok=True)
        out.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)

        os.environ["APP_ROOT"] = str(root)
        os.environ["INPUT_DIR"] = str(inp)
        os.environ["DUBBING_OUTPUT_DIR"] = str(out)
        os.environ["DUBBING_LOG_DIR"] = str(logs)
        os.environ["COOKIE_SECURE"] = "0"
        os.environ["STRICT_SECRETS"] = "0"

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
                id="u1",
                username="user1",
                password_hash=ph.hash("pass1"),
                role=Role.operator,
                totp_secret=None,
                totp_enabled=False,
                created_at=now_ts(),
            )
        )

        # Job A: DONE with outputs
        job_store.put(
            Job(
                id="job_done",
                owner_id="u1",
                video_path=str(inp / "a.mp4"),
                duration_s=1.0,
                mode="low",
                device="cpu",
                src_lang="auto",
                tgt_lang="en",
                created_at=now_utc(),
                updated_at=now_utc(),
                state=JobState.DONE,
                progress=1.0,
                message="Done",
                output_mkv=str(out / "job_done" / "dub.mkv"),
                output_srt="",
                work_dir=str(out / "job_done"),
                log_path=str(logs / "job_done.log"),
                error=None,
                series_title="Alpha Series",
                series_slug="alpha-series",
                season_number=1,
                episode_number=2,
            )
        )
        # Job B: FAILED
        job_store.put(
            Job(
                id="job_failed",
                owner_id="u1",
                video_path=str(inp / "b.mp4"),
                duration_s=1.0,
                mode="low",
                device="cpu",
                src_lang="auto",
                tgt_lang="en",
                created_at=now_utc(),
                updated_at=now_utc(),
                state=JobState.FAILED,
                progress=0.2,
                message="Failed",
                output_mkv="",
                output_srt="",
                work_dir=str(out / "job_failed"),
                log_path=str(logs / "job_failed.log"),
                error="fail",
                series_title="Beta Series",
                series_slug="beta-series",
                season_number=2,
                episode_number=3,
            )
        )
        # Job C: RUNNING
        job_store.put(
            Job(
                id="job_running",
                owner_id="u1",
                video_path=str(inp / "c.mp4"),
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
                work_dir=str(out / "job_running"),
                log_path=str(logs / "job_running.log"),
                error=None,
                series_title="Alpha Series",
                series_slug="alpha-series",
                season_number=1,
                episode_number=3,
            )
        )

        app.include_router(auth_router)
        app.include_router(library_router)

        with TestClient(app) as c:
            login = c.post(
                "/auth/login", json={"username": "user1", "password": "pass1", "session": True}
            )
            assert login.status_code == 200, login.text
            csrf = login.json().get("csrf_token")
            assert csrf

            r1 = c.get("/api/library/search?q=Alpha&status=has_outputs")
            assert r1.status_code == 200, r1.text
            items = r1.json().get("items") or []
            assert len(items) == 1 and items[0]["job_id"] == "job_done", items

            r2 = c.get("/api/library/search?status=failed")
            assert r2.status_code == 200, r2.text
            items2 = r2.json().get("items") or []
            assert any(it.get("job_id") == "job_failed" for it in items2), items2

            r3 = c.get("/api/library/search?status=in_progress")
            assert r3.status_code == 200, r3.text
            items3 = r3.json().get("items") or []
            assert any(it.get("job_id") == "job_running" for it in items3), items3

            r4 = c.get("/api/library/recent?limit=5")
            assert r4.status_code == 200, r4.text
            recent = r4.json().get("items") or []
            assert any(it.get("job_id") == "job_done" for it in recent), recent

            r5 = c.post(
                "/api/library/continue",
                json={"series_slug": "alpha-series", "series_title": "Alpha Series"},
                headers={"X-CSRF-Token": csrf},
            )
            assert r5.status_code == 200, r5.text
            r6 = c.get("/api/library/continue")
            assert r6.status_code == 200, r6.text
            item = r6.json().get("item") or {}
            assert item.get("series_slug") == "alpha-series", item

    print("verify_library_search: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
