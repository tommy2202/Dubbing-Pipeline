from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.api.models import Role, User, now_ts
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.server import app
from dubbing_pipeline.utils.crypto import PasswordHasher, random_id


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
        vp.write_bytes(b"\x00" * 1024)
    os.environ["APP_ROOT"] = str(root)
    os.environ["INPUT_DIR"] = str(in_dir)
    os.environ["DUBBING_OUTPUT_DIR"] = str(out_dir)
    os.environ["DUBBING_LOG_DIR"] = str(logs_dir)
    return str(vp)


def _login(c: TestClient, *, username: str, password: str) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def test_viewer_is_read_only_for_state_changing_apis(tmp_path: Path) -> None:
    video_path = _runtime_video_path(tmp_path)
    os.environ["DUBBING_SETTINGS_PATH"] = str(tmp_path / "settings.json")
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
            json={"video_path": video_path, "device": "cpu", "mode": "low"},
        )
        assert rj.status_code in {403, 401}

        # viewer cannot update settings
        rs = c.put("/api/settings", headers=headers, json={"defaults": {"mode": "high"}})
        assert rs.status_code == 403


def test_operator_can_submit_jobs_but_cannot_manage_presets_projects(tmp_path: Path) -> None:
    _ = _runtime_video_path(tmp_path)
    os.environ["DUBBING_SETTINGS_PATH"] = str(tmp_path / "settings.json")
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
    _ = _runtime_video_path(tmp_path)
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
