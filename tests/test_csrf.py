from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.server import app


def _set_env(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    in_dir = root / "Input"
    out_dir = root / "Output"
    logs_dir = root / "logs"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    os.environ["APP_ROOT"] = str(root)
    os.environ["INPUT_DIR"] = str(in_dir)
    os.environ["DUBBING_OUTPUT_DIR"] = str(out_dir)
    os.environ["DUBBING_LOG_DIR"] = str(logs_dir)
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"


def test_csrf_required_for_cookie_post(tmp_path: Path) -> None:
    _set_env(tmp_path)
    get_settings.cache_clear()

    with TestClient(app) as c:
        login = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
        assert login.status_code == 200, login.text
        csrf = login.json().get("csrf_token")

        r1 = c.post("/api/auth/logout", headers={"Origin": "https://evil.example"})
        assert r1.status_code == 403

        r2 = c.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": str(csrf), "Origin": "https://ui.example"},
        )
        assert r2.status_code == 200
