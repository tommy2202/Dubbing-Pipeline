from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState
from dubbing_pipeline.server import app


def _runtime_video_path(tmp_path: Path) -> str:
    root = tmp_path.resolve()
    in_dir = root / "Input"
    out_dir = root / "Output"
    logs_dir = root / "logs"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    vp = in_dir / "Test.mp4"
    if not vp.exists():
        # Minimal placeholder file; tests validate path safety, not media decoding.
        vp.write_bytes(b"\x00" * 1024)
    os.environ["APP_ROOT"] = str(root)
    os.environ["INPUT_DIR"] = str(in_dir)
    os.environ["DUBBING_OUTPUT_DIR"] = str(out_dir)
    os.environ["DUBBING_LOG_DIR"] = str(logs_dir)
    return str(vp)


def _login_admin(c: TestClient) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    assert r.status_code == 200
    data = r.json()
    # TestClient persists cookies; include CSRF header to satisfy cookie-session CSRF enforcement.
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def test_api_jobs_list_supports_filters_and_pagination(tmp_path: Path) -> None:
    video_path = _runtime_video_path(tmp_path)
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
                video_path=video_path,
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
    video_path = _runtime_video_path(tmp_path)
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
                video_path=video_path,
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
