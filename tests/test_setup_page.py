from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.server import app


def _setup_env(tmp_path: Path, *, admin_password: str = "adminpass", ffmpeg_bin: str | None = None) -> None:
    os.environ["DUBBING_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["APP_ROOT"] = str(tmp_path)
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = admin_password
    os.environ["COOKIE_SECURE"] = "0"
    if ffmpeg_bin:
        os.environ["FFMPEG_BIN"] = ffmpeg_bin
    else:
        os.environ.pop("FFMPEG_BIN", None)
    get_settings.cache_clear()


def _login_admin(client: TestClient, *, password: str) -> None:
    r = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": password, "session": True},
    )
    assert r.status_code == 200, r.text


def test_setup_page_renders_when_logged_in(tmp_path: Path) -> None:
    _setup_env(tmp_path)
    with TestClient(app) as c:
        _login_admin(c, password="adminpass")
        r = c.get("/ui/setup")
        assert r.status_code == 200
        assert "Setup / Health" in r.text


def test_setup_page_missing_deps_show_missing(tmp_path: Path) -> None:
    _setup_env(tmp_path, ffmpeg_bin="missing_ffmpeg")
    with TestClient(app) as c:
        _login_admin(c, password="adminpass")
        r = c.get("/ui/setup")
        assert r.status_code == 200
        assert "ffmpeg" in r.text
        assert "MISSING" in r.text


def test_setup_page_does_not_leak_secrets(tmp_path: Path) -> None:
    secret = "supersecretvalue"
    _setup_env(tmp_path, admin_password=secret)
    with TestClient(app) as c:
        _login_admin(c, password=secret)
        r = c.get("/ui/setup")
        assert r.status_code == 200
        assert secret not in r.text
