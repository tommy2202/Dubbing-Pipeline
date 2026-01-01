from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from anime_v2.config import get_settings
from anime_v2.server import app


def _login_admin(c: TestClient) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    assert r.status_code == 200
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def test_user_settings_get_put_and_upload_defaults(tmp_path: Path) -> None:
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["ANIME_V2_SETTINGS_PATH"] = str(tmp_path / "settings.json")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        headers = _login_admin(c)

        r0 = c.get("/api/settings", headers=headers)
        assert r0.status_code == 200
        d0 = r0.json()
        assert "defaults" in d0 and isinstance(d0["defaults"], dict)

        r1 = c.put(
            "/api/settings",
            headers=headers,
            json={
                "defaults": {
                    "mode": "high",
                    "device": "cpu",
                    "src_lang": "ja",
                    "tgt_lang": "en",
                    "tts_lang": "en",
                    "tts_speaker": "default",
                },
                "notifications": {"discord_webhook": ""},
            },
        )
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["defaults"]["mode"] == "high"
        assert d1["defaults"]["device"] == "cpu"

        r2 = c.get("/api/settings", headers=headers)
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["defaults"]["mode"] == "high"

        # Upload wizard should reflect settings server-side (no JS required).
        html = c.get("/ui/upload", headers=headers).text
        assert "mode: 'high'" in html or 'mode: "high"' in html

        # Settings page renders.
        html2 = c.get("/ui/settings", headers=headers).text
        assert "Settings" in html2


def test_discord_webhook_test_requires_configured_url(tmp_path: Path) -> None:
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["ANIME_V2_SETTINGS_PATH"] = str(tmp_path / "settings.json")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        headers = _login_admin(c)
        r = c.post("/api/settings/notifications/test", headers=headers)
        assert r.status_code == 400

