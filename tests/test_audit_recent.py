from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.server import app


def test_audit_recent_filters_to_current_user(tmp_path: Path) -> None:
    os.environ["DUBBING_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["DUBBING_LOG_DIR"] = str(tmp_path / "logs")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        # login writes audit events with user_id
        r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
        assert r.status_code == 200
        data = r.json()
        headers = {
            "Authorization": f"Bearer {data['access_token']}",
            "X-CSRF-Token": data["csrf_token"],
        }

        r2 = c.get("/api/audit/recent?limit=50", headers=headers)
        assert r2.status_code == 200
        d2 = r2.json()
        items = d2.get("items")
        assert isinstance(items, list)
        # At least one auth event should exist.
        assert any(isinstance(it, dict) and (it.get("event") == "auth.login_ok") for it in items)
