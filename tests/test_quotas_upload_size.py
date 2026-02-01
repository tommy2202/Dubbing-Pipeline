from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from dubbing_pipeline.api.models import Role, User, now_ts
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.server import app
from dubbing_pipeline.utils.crypto import PasswordHasher, random_id
from tests._helpers.auth import login_user
from tests._helpers.redis import redis_available
from tests._helpers.runtime_paths import configure_runtime_paths


def _make_user(*, username: str, password: str, role: Role) -> User:
    ph = PasswordHasher()
    return User(
        id=random_id("u_", 16),
        username=username,
        password_hash=ph.hash(password),
        role=role,
        totp_secret=None,
        totp_enabled=False,
        created_at=now_ts(),
    )


@pytest.mark.parametrize("redis_enabled", [False, True])
def test_upload_size_limit(tmp_path, monkeypatch, redis_enabled: bool) -> None:
    configure_runtime_paths(tmp_path)
    if redis_enabled:
        get_settings.cache_clear()
        if not redis_available():
            pytest.skip("redis not available")
    else:
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("QUEUE_BACKEND", raising=False)
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "64")
    monkeypatch.setenv("MAX_STORAGE_BYTES_PER_USER", "100000")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("MIN_FREE_GB", "0")
    get_settings.cache_clear()

    with TestClient(app) as c:
        auth = c.app.state.auth_store
        user = _make_user(username="upload_quota", password="pass", role=Role.operator)
        auth.upsert_user(user)
        headers = login_user(c, username="upload_quota", password="pass", clear_cookies=True)

        resp = c.post(
            "/api/uploads/init",
            headers=headers,
            json={"filename": "clip.mp4", "total_bytes": 128},
        )
        assert resp.status_code == 429, resp.text
        detail = resp.json()
        assert detail.get("error") == "quota_exceeded"
        assert detail.get("code") == "upload_bytes_limit"
