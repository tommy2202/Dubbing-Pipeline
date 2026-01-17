from __future__ import annotations

import hashlib
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


def _login_admin(c: TestClient) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def test_upload_traversal_rejected(tmp_path: Path) -> None:
    _set_env(tmp_path)
    os.environ["MAX_UPLOAD_BYTES"] = "1048576"
    get_settings.cache_clear()

    with TestClient(app) as c:
        headers = _login_admin(c)
        r = c.post(
            "/api/uploads/init",
            json={"filename": "../evil.mp4", "total_bytes": 8, "mime": "video/mp4"},
            headers=headers,
        )
        assert r.status_code == 400


def test_upload_oversize_rejected(tmp_path: Path) -> None:
    _set_env(tmp_path)
    os.environ["MAX_UPLOAD_BYTES"] = "10"
    get_settings.cache_clear()

    with TestClient(app) as c:
        headers = _login_admin(c)
        r = c.post(
            "/api/uploads/init",
            json={"filename": "big.mp4", "total_bytes": 1024, "mime": "video/mp4"},
            headers=headers,
        )
        assert r.status_code == 413


def test_upload_bad_chunk_order(tmp_path: Path) -> None:
    _set_env(tmp_path)
    os.environ["MAX_UPLOAD_BYTES"] = "1048576"
    os.environ["UPLOAD_CHUNK_BYTES"] = "4"
    get_settings.cache_clear()

    with TestClient(app) as c:
        headers = _login_admin(c)
        init = c.post(
            "/api/uploads/init",
            json={"filename": "clip.mp4", "total_bytes": 8, "mime": "video/mp4"},
            headers=headers,
        )
        assert init.status_code == 200, init.text
        upload_id = init.json()["upload_id"]

        body = b"abcd"
        sha = hashlib.sha256(body).hexdigest()
        r = c.post(
            f"/api/uploads/{upload_id}/chunk?index=1&offset=4",
            data=body,
            headers={**headers, "X-Chunk-Sha256": sha},
        )
        assert r.status_code == 409
