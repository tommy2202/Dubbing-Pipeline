from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from dubbing_pipeline.api.models import Role, User, now_ts
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.server import app
from dubbing_pipeline.utils.crypto import PasswordHasher, random_id
from tests._helpers.auth import login_user
from tests._helpers.redis import redis_available
from tests._helpers.runtime_paths import configure_runtime_paths


def _runtime_video_path(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")
    in_dir, _out_dir, _logs_dir = configure_runtime_paths(tmp_path)
    vp = in_dir / "Test.mp4"
    if not vp.exists():
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=160x90:rate=10",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t",
                "1.0",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(vp),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return vp


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
def test_jobs_per_day_limit(tmp_path, monkeypatch, redis_enabled: bool) -> None:
    video_path = _runtime_video_path(tmp_path)
    if redis_enabled:
        get_settings.cache_clear()
        if not redis_available():
            pytest.skip("redis not available")
        monkeypatch.setenv("QUEUE_BACKEND", "redis")
    else:
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("QUEUE_BACKEND", raising=False)
    monkeypatch.setenv("JOBS_PER_DAY_PER_USER", "1")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10000000")
    monkeypatch.setenv("MAX_STORAGE_BYTES_PER_USER", "100000000")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("MIN_FREE_GB", "0")
    get_settings.cache_clear()

    with TestClient(app) as c:
        auth = c.app.state.auth_store
        user = _make_user(username="quota_daily", password="pass", role=Role.operator)
        auth.upsert_user(user)
        headers = login_user(c, username="quota_daily", password="pass", clear_cookies=True)

        payload = {
            "video_path": str(video_path),
            "device": "cpu",
            "mode": "low",
            "series_title": "Series Q",
            "season_number": 1,
            "episode_number": 1,
        }
        r1 = c.post("/api/jobs", headers=headers, json=payload)
        assert r1.status_code == 200, r1.text

        payload["episode_number"] = 2
        r2 = c.post("/api/jobs", headers=headers, json=payload)
        assert r2.status_code == 429, r2.text
        detail = r2.json()
        assert detail.get("error") == "quota_exceeded"
        assert detail.get("code") == "jobs_per_day_limit"
