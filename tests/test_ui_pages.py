from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from anime_v2.config import get_settings
from anime_v2.server import app


def test_ui_login_renders_csrf(tmp_path: Path) -> None:
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(tmp_path / "Output")
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
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        r = c.get("/ui/dashboard", follow_redirects=False)
        assert r.status_code in {301, 302, 307, 308}


def test_ui_dashboard_renders_when_logged_in(tmp_path: Path) -> None:
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass", "session": True})
        assert r.status_code == 200
        d = c.get("/ui/dashboard")
        assert d.status_code == 200
        assert "Dashboard" in d.text

