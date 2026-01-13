from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from anime_v2.config import get_settings
from anime_v2.runtime import lifecycle
from anime_v2.server import app


def _runtime_video_path(tmp_path: Path, *, output_dir_name: str = "Output") -> str:
    root = tmp_path.resolve()
    in_dir = root / "Input"
    out_dir = root / output_dir_name
    logs_dir = root / "logs"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    vp = in_dir / "Test.mp4"
    if not vp.exists():
        vp.write_bytes(b"\x00" * 1024)
    os.environ["APP_ROOT"] = str(root)
    os.environ["INPUT_DIR"] = str(in_dir)
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(out_dir)
    os.environ["ANIME_V2_LOG_DIR"] = str(logs_dir)
    return str(vp)


def test_readyz_503_when_draining(tmp_path: Path) -> None:
    lifecycle.end_draining()
    _ = _runtime_video_path(tmp_path)
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
    video_path = _runtime_video_path(tmp_path, output_dir_name="Output2")
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
            json={"video_path": video_path, "device": "cpu", "mode": "low"},
        )
        assert r2.status_code == 503
    lifecycle.end_draining()
