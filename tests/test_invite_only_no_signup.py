from __future__ import annotations

import os

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.server import app
from tests._helpers.auth import login_admin


def _configure_admin() -> None:
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()


def test_signup_register_endpoints_disabled() -> None:
    _configure_admin()
    paths = ["/auth/register", "/auth/signup", "/api/auth/register", "/api/auth/signup"]
    with TestClient(app) as c:
        for path in paths:
            for method in ("get", "post"):
                resp = getattr(c, method)(path)
                assert resp.status_code in {401, 403, 404}

        # Even with admin auth, there is no alternate signup route.
        headers = login_admin(c)
        for path in paths:
            resp = c.post(
                path,
                headers=headers,
                json={"username": "noinvite", "password": "password123"},
            )
            assert resp.status_code == 404


def test_invite_redeem_requires_valid_token() -> None:
    _configure_admin()
    with TestClient(app) as c:
        resp = c.post(
            "/api/invites/redeem",
            json={"token": "invalid_token", "username": "noinvite", "password": "password123"},
        )
        assert resp.status_code in {400, 401, 410}

        login = c.post(
            "/api/auth/login", json={"username": "noinvite", "password": "password123"}
        )
        assert login.status_code == 401


def test_invite_create_requires_admin() -> None:
    _configure_admin()
    with TestClient(app) as c:
        resp = c.post("/api/admin/invites", json={"expires_in_hours": 1})
        assert resp.status_code in {401, 403}

        headers = login_admin(c)
        ok = c.post("/api/admin/invites", headers=headers, json={"expires_in_hours": 1})
        assert ok.status_code == 200
