from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from anime_v2.api.models import Role, User, now_ts
from anime_v2.config import get_settings
from anime_v2.server import app
from anime_v2.utils.crypto import PasswordHasher, random_id


def _login(c: TestClient, *, username: str, password: str) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def test_viewer_is_read_only_for_state_changing_apis(tmp_path: Path) -> None:
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["ANIME_V2_SETTINGS_PATH"] = str(tmp_path / "settings.json")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        # create viewer user
        store = c.app.state.auth_store
        ph = PasswordHasher()
        viewer = User(
            id=random_id("u_", 16),
            username="viewer1",
            password_hash=ph.hash("viewerpass"),
            role=Role.viewer,
            totp_secret=None,
            totp_enabled=False,
            created_at=now_ts(),
        )
        store.upsert_user(viewer)

        headers = _login(c, username="viewer1", password="viewerpass")

        # viewer cannot submit jobs
        rj = c.post(
            "/api/jobs",
            headers=headers,
            json={"video_path": "/workspace/Input/Test.mp4", "device": "cpu", "mode": "low"},
        )
        assert rj.status_code in {403, 401}

        # viewer cannot update settings
        rs = c.put("/api/settings", headers=headers, json={"defaults": {"mode": "high"}})
        assert rs.status_code == 403


def test_operator_can_submit_jobs_but_cannot_manage_presets_projects(tmp_path: Path) -> None:
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["ANIME_V2_SETTINGS_PATH"] = str(tmp_path / "settings.json")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["MIN_FREE_GB"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        store = c.app.state.auth_store
        ph = PasswordHasher()
        op = User(
            id=random_id("u_", 16),
            username="op1",
            password_hash=ph.hash("oppass"),
            role=Role.operator,
            totp_secret=None,
            totp_enabled=False,
            created_at=now_ts(),
        )
        store.upsert_user(op)
        headers = _login(c, username="op1", password="oppass")

        # operator can update own settings
        rs = c.put("/api/settings", headers=headers, json={"defaults": {"mode": "high"}})
        assert rs.status_code == 200

        # operator cannot manage presets/projects (admin only)
        rp = c.post("/api/presets", headers=headers, json={"name": "x"})
        assert rp.status_code == 403
        rpr = c.post("/api/projects", headers=headers, json={"name": "x"})
        assert rpr.status_code == 403


def test_admin_state_change_requires_csrf_when_using_cookies(tmp_path: Path) -> None:
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        # login sets cookies (refresh + csrf); omit CSRF header to ensure 403
        r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
        assert r.status_code == 200
        token = r.json()["access_token"]

        r2 = c.post(
            "/api/presets", headers={"Authorization": f"Bearer {token}"}, json={"name": "no_csrf"}
        )
        assert r2.status_code == 403
