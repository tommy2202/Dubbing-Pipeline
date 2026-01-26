from __future__ import annotations

import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from dubbing_pipeline.api.models import Role, User, now_ts
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState
from dubbing_pipeline.server import app
from dubbing_pipeline.utils.crypto import PasswordHasher, random_id
from tests._helpers.auth import login_user
from tests._helpers.redis import redis_available, redis_client, redis_prefix
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
def test_concurrent_jobs_limit(tmp_path, monkeypatch, redis_enabled: bool) -> None:
    video_path = _runtime_video_path(tmp_path)
    if redis_enabled:
        get_settings.cache_clear()
        if not redis_available():
            pytest.skip("redis not available")
        monkeypatch.setenv("QUEUE_BACKEND", "redis")
    else:
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("QUEUE_BACKEND", raising=False)
    monkeypatch.setenv("MAX_CONCURRENT_JOBS_PER_USER", "1")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10000000")
    monkeypatch.setenv("MAX_STORAGE_BYTES_PER_USER", "100000000")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("MIN_FREE_GB", "0")
    get_settings.cache_clear()

    with TestClient(app) as c:
        store = c.app.state.job_store
        auth = c.app.state.auth_store
        user = _make_user(username="quota_run", password="pass", role=Role.operator)
        auth.upsert_user(user)
        headers = login_user(c, username="quota_run", password="pass", clear_cookies=True)

        running = Job(
            id="job_running_quota",
            owner_id=user.id,
            video_path=str(video_path),
            duration_s=1.0,
            mode="low",
            device="cpu",
            src_lang="ja",
            tgt_lang="en",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            state=JobState.RUNNING,
            progress=0.5,
            message="running",
            output_mkv="",
            output_srt="",
            work_dir="",
            log_path="",
            error=None,
            series_title="Series R",
            series_slug="series-r",
            season_number=1,
            episode_number=1,
        )
        store.put(running)

        if redis_enabled:
            client = redis_client()
            if client is None:
                pytest.skip("redis client unavailable")
            prefix = redis_prefix()
            running_key = f"{prefix}:user:{user.id}:running"
            client.sadd(running_key, running.id)
            client.expire(running_key, 60)

        payload = {
            "video_path": str(video_path),
            "device": "cpu",
            "mode": "low",
            "series_title": "Series Q",
            "season_number": 1,
            "episode_number": 2,
        }
        resp = c.post("/api/jobs", headers=headers, json=payload)
        assert resp.status_code == 429, resp.text
        detail = resp.json().get("detail", {})
        assert detail.get("error") == "quota_exceeded"
        assert detail.get("reason") == "concurrent_jobs_limit"
