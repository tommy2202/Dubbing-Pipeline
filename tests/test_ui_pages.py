from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState
from dubbing_pipeline.server import app


def test_ui_login_renders_csrf(tmp_path: Path) -> None:
    os.environ["DUBBING_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        r = c.get("/ui/login")
        assert r.status_code == 200
        assert 'id="csrf"' in r.text


def test_ui_dashboard_redirects_when_not_logged_in(tmp_path: Path) -> None:
    os.environ["DUBBING_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        r = c.get("/ui/dashboard", follow_redirects=False)
        assert r.status_code in {301, 302, 307, 308}


def test_ui_dashboard_renders_when_logged_in(tmp_path: Path) -> None:
    os.environ["DUBBING_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        r = c.post(
            "/api/auth/login", json={"username": "admin", "password": "adminpass", "session": True}
        )
        assert r.status_code == 200
        d = c.get("/ui/dashboard")
        assert d.status_code == 200
        assert "Dashboard" in d.text


def test_ui_job_detail_shows_created_toast(tmp_path: Path) -> None:
    os.environ["DUBBING_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        r = c.post(
            "/api/auth/login", json={"username": "admin", "password": "adminpass", "session": True}
        )
        assert r.status_code == 200
        store = c.app.state.job_store
        now = "2026-01-01T00:00:00+00:00"
        out_dir = Path(os.environ["DUBBING_OUTPUT_DIR"]) / "Test"
        out_dir.mkdir(parents=True, exist_ok=True)
        store.put(
            Job(
                id="abc123",
                owner_id="u1",
                video_path=str(tmp_path / "Input" / "Test.mp4"),
                duration_s=1.0,
                mode="low",
                device="cpu",
                src_lang="ja",
                tgt_lang="en",
                created_at=now,
                updated_at=now,
                state=JobState.DONE,
                progress=1.0,
                message="Done",
                output_mkv="",
                output_srt="",
                work_dir=str(out_dir),
                log_path=str(out_dir / "job.log"),
            )
        )
        d = c.get("/ui/jobs/abc123?created=1")
        assert d.status_code == 200
        assert "Job created." in d.text
