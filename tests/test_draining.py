from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from anime_v2.config import get_settings
from anime_v2.runtime import lifecycle
from anime_v2.server import app


def test_readyz_503_when_draining(tmp_path: Path) -> None:
    lifecycle.end_draining()
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        lifecycle.begin_draining(timeout_sec=5)
        r = c.get("/readyz")
        assert r.status_code == 503
    lifecycle.end_draining()


def test_job_submit_503_when_draining(tmp_path: Path) -> None:
    lifecycle.end_draining()
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(tmp_path / "Output2")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        r = c.post(
            "/auth/login", json={"username": "admin", "password": "adminpass", "session": True}
        )
        token = r.json()["access_token"]
        csrf = r.json()["csrf_token"]
        lifecycle.begin_draining(timeout_sec=5)
        r2 = c.post(
            "/api/jobs",
            headers={"Authorization": f"Bearer {token}", "X-CSRF-Token": csrf},
            json={"video_path": "/workspace/Input/Test.mp4", "device": "cpu", "mode": "low"},
        )
        assert r2.status_code == 503
    lifecycle.end_draining()
