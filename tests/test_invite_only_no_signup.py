from __future__ import annotations

import os
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.server import app
from dubbing_pipeline.utils.route_map import collect_route_details
from tests._helpers.auth import login_admin


def _configure_admin() -> None:
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()


def _signup_route_candidates() -> list[tuple[str, str]]:
    candidates: set[tuple[str, str]] = set()
    for item in collect_route_details(app):
        path = item["path"]
        method = item["method"]
        if any(tok in path for tok in ("/signup", "/register", "/create-user", "/create_user")):
            candidates.add((method, path))
            continue
        if method == "POST" and re.search(r"/users/?$", path):
            candidates.add((method, path))
    return sorted(candidates)


def test_signup_register_endpoints_disabled() -> None:
    _configure_admin()
    candidates = _signup_route_candidates()
    if candidates:
        with TestClient(app) as c:
            for _method, path in candidates:
                resp = c.post(path)
                assert resp.status_code in {403, 404}
        pytest.fail(f"Self-signup routes should not exist: {candidates}")


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


def test_invite_redeem_creates_user() -> None:
    _configure_admin()
    with TestClient(app) as c:
        headers = login_admin(c)
        invite = c.post("/api/admin/invites", headers=headers, json={"expires_in_hours": 1})
        assert invite.status_code == 200
        invite_url = invite.json().get("invite_url", "")
        token = str(invite_url).split("/invite/")[-1].strip()
        assert token

        username = f"user_{uuid.uuid4().hex[:8]}"
        password = "password123"
        redeem = c.post(
            "/api/invites/redeem",
            json={"token": token, "username": username, "password": password},
        )
        assert redeem.status_code == 200
        login = c.post("/api/auth/login", json={"username": username, "password": password})
        assert login.status_code == 200


def test_invite_create_requires_admin() -> None:
    _configure_admin()
    with TestClient(app) as c:
        resp = c.post("/api/admin/invites", json={"expires_in_hours": 1})
        assert resp.status_code in {401, 403}

        headers = login_admin(c)
        ok = c.post("/api/admin/invites", headers=headers, json={"expires_in_hours": 1})
        assert ok.status_code == 200
