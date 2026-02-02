from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.api.models import Role, now_ts
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.server import app
from dubbing_pipeline.utils.crypto import PasswordHasher


def _setup_env(tmp_path: Path) -> None:
    os.environ["DUBBING_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()


def _login_operator(client: TestClient) -> None:
    store = client.app.state.auth_store
    store.create_user(
        username="operator",
        password_hash=PasswordHasher().hash("operatorpass"),
        role=Role.operator,
        created_at=now_ts(),
    )
    resp = client.post(
        "/api/auth/login",
        json={"username": "operator", "password": "operatorpass", "session": True},
    )
    assert resp.status_code == 200, resp.text


def test_simple_upload_page_renders(tmp_path: Path) -> None:
    _setup_env(tmp_path)
    with TestClient(app) as c:
        _login_operator(c)
        r = c.get("/ui/upload")
        assert r.status_code == 200
        assert "Simple" in r.text
        assert "Upload" in r.text


def test_admin_login_button_present_for_non_admin(tmp_path: Path) -> None:
    _setup_env(tmp_path)
    with TestClient(app) as c:
        _login_operator(c)
        r = c.get("/ui/upload")
        assert r.status_code == 200
        assert "Admin Login" in r.text


def test_job_submit_requires_series_fields(tmp_path: Path) -> None:
    _setup_env(tmp_path)
    with TestClient(app) as c:
        r = c.post(
            "/api/auth/login",
            json={"username": "admin", "password": "adminpass", "session": True},
        )
        assert r.status_code == 200, r.text
        resp = c.post("/api/jobs", json={"video_path": "/tmp/does-not-matter"})
        assert resp.status_code == 422


def test_admin_pages_forbidden_for_non_admin(tmp_path: Path) -> None:
    _setup_env(tmp_path)
    with TestClient(app) as c:
        _login_operator(c)
        r = c.get("/ui/admin/dashboard")
        assert r.status_code == 403
