from __future__ import annotations

import os

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.server import app


def _login_admin(c: TestClient) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    assert r.status_code == 200
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def test_invite_redeem_flow() -> None:
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        headers = _login_admin(c)

        reg = c.post("/api/auth/register", json={"username": "self", "password": "password123"})
        assert reg.status_code in {403, 404}

        inv = c.post("/api/admin/invites", headers=headers, json={"expires_in_hours": 1})
        assert inv.status_code == 200
        inv_data = inv.json()
        invite_url = str(inv_data.get("invite_url") or "")
        assert invite_url
        token = invite_url.rsplit("/", 1)[-1]

        ui = c.get(f"/invite/{token}")
        assert ui.status_code == 200

        redeem = c.post(
            "/api/invites/redeem",
            json={"token": token, "username": "invited_user", "password": "passw0rd123"},
        )
        assert redeem.status_code == 200

        login = c.post(
            "/api/auth/login", json={"username": "invited_user", "password": "passw0rd123"}
        )
        assert login.status_code == 200

        redeem_again = c.post(
            "/api/invites/redeem",
            json={"token": token, "username": "invited_user2", "password": "passw0rd123"},
        )
        assert redeem_again.status_code in {400, 410}

