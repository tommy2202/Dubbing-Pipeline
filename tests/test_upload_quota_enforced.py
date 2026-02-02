from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.server import app
from tests._helpers.auth import login_admin


def _setup_env(tmp_path: Path) -> None:
    os.environ["APP_ROOT"] = str(tmp_path)
    os.environ["DUBBING_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["MAX_UPLOAD_BYTES"] = "8"
    get_settings.cache_clear()


def test_upload_quota_enforced(tmp_path: Path) -> None:
    _setup_env(tmp_path)
    with TestClient(app) as c:
        headers = login_admin(c)
        resp = c.post(
            "/api/uploads/init",
            json={"filename": "clip.mp4", "total_bytes": 64, "mime": "video/mp4"},
            headers=headers,
        )
        assert resp.status_code == 429
        data = resp.json()
        assert data.get("error") == "quota_exceeded"
        assert data.get("code") == "upload_bytes_limit"
