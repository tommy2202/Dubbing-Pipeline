from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from anime_v2.config import get_settings
from anime_v2.server import app


def test_idempotency_key_returns_same_job_id(tmp_path: Path) -> None:
    # isolate Output root for test
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        r = c.post("/auth/login", json={"username": "admin", "password": "adminpass"})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}", "X-CSRF-Token": r.json()["csrf_token"], "Idempotency-Key": "abc123"}

        r1 = c.post("/api/jobs", headers=headers, json={"video_path": "/workspace/Input/Test.mp4", "device": "cpu", "mode": "low"})
        r2 = c.post("/api/jobs", headers=headers, json={"video_path": "/workspace/Input/Test.mp4", "device": "cpu", "mode": "low"})
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["id"] == r2.json()["id"]

