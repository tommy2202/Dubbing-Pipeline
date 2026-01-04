from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from anime_v2.config import get_settings
from anime_v2.jobs.models import Job, JobState
from anime_v2.server import app


def _login_admin(c: TestClient) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    assert r.status_code == 200
    data = r.json()
    # TestClient persists cookies; include CSRF header to satisfy cookie-session CSRF enforcement.
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def test_api_jobs_list_supports_filters_and_pagination(tmp_path: Path) -> None:
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        headers = _login_admin(c)
        store = c.app.state.job_store
        now = "2026-01-01T00:00:00+00:00"
        store.put(
            Job(
                id="j_test_1",
                owner_id="u1",
                video_path="/workspace/Input/Test.mp4",
                duration_s=10.0,
                mode="low",
                device="cpu",
                src_lang="ja",
                tgt_lang="en",
                created_at=now,
                updated_at=now,
                state=JobState.QUEUED,
                progress=0.0,
                message="Queued",
                output_mkv="",
                output_srt="",
                work_dir="",
                log_path=str(tmp_path / "job.log"),
            )
        )

        r = c.get("/api/jobs?status=QUEUED&limit=10&offset=0&q=Test.mp4", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert any(it["id"] == "j_test_1" for it in data["items"])


def test_pause_resume_endpoints_for_queued_job(tmp_path: Path) -> None:
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        headers = _login_admin(c)
        store = c.app.state.job_store
        now = "2026-01-01T00:00:00+00:00"
        store.put(
            Job(
                id="j_pause_1",
                owner_id="u1",
                video_path="/workspace/Input/Test.mp4",
                duration_s=10.0,
                mode="low",
                device="cpu",
                src_lang="ja",
                tgt_lang="en",
                created_at=now,
                updated_at=now,
                state=JobState.QUEUED,
                progress=0.0,
                message="Queued",
                output_mkv="",
                output_srt="",
                work_dir="",
                log_path=str(tmp_path / "job.log"),
            )
        )
        r1 = c.post("/api/jobs/j_pause_1/pause", headers=headers)
        assert r1.status_code == 200
        assert r1.json()["state"] == "PAUSED"
        r2 = c.post("/api/jobs/j_pause_1/resume", headers=headers)
        assert r2.status_code == 200
        assert r2.json()["state"] == "QUEUED"
