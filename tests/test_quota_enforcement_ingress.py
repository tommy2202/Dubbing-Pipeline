from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.api.models import Role, User, now_ts
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState
from dubbing_pipeline.server import app
from dubbing_pipeline.utils.crypto import PasswordHasher, random_id
from tests._helpers.auth import login_user
from tests._helpers.media import ensure_tiny_mp4
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


def _assert_quota_response(resp, *, code: str, limit: int | None = None, reset_min: int = 0) -> None:
    assert resp.status_code == 429, resp.text
    payload = resp.json()
    assert payload.get("error") == "quota_exceeded"
    assert payload.get("code") == code
    assert "detail" in payload
    assert "reset_seconds" in payload
    assert int(payload.get("reset_seconds") or 0) >= int(reset_min)
    assert "remaining" in payload
    if limit is not None:
        assert payload.get("limit") == limit


def test_quota_upload_ingress(tmp_path: Path, monkeypatch) -> None:
    configure_runtime_paths(tmp_path)
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "64")
    monkeypatch.setenv("MAX_STORAGE_BYTES_PER_USER", "100000")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("MIN_FREE_GB", "0")
    get_settings.cache_clear()

    with TestClient(app) as c:
        auth = c.app.state.auth_store
        user = _make_user(username="quota_upload_ingress", password="pass", role=Role.operator)
        auth.upsert_user(user)
        headers = login_user(c, username="quota_upload_ingress", password="pass", clear_cookies=True)

        resp = c.post(
            "/api/uploads/init",
            headers=headers,
            json={"filename": "clip.mp4", "total_bytes": 128},
        )
        _assert_quota_response(resp, code="upload_bytes_limit", limit=64)


def test_quota_jobs_per_day_ingress(tmp_path: Path, monkeypatch) -> None:
    in_dir, _out_dir, _logs_dir = configure_runtime_paths(tmp_path)
    video_path = ensure_tiny_mp4(in_dir / "Test.mp4", skip_message="ffmpeg not available")
    monkeypatch.setenv("JOBS_PER_DAY_PER_USER", "1")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10000000")
    monkeypatch.setenv("MAX_STORAGE_BYTES_PER_USER", "100000000")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("MIN_FREE_GB", "0")
    get_settings.cache_clear()

    with TestClient(app) as c:
        auth = c.app.state.auth_store
        user = _make_user(username="quota_daily_ingress", password="pass", role=Role.operator)
        auth.upsert_user(user)
        headers = login_user(c, username="quota_daily_ingress", password="pass", clear_cookies=True)

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
        _assert_quota_response(r2, code="jobs_per_day_limit", limit=1, reset_min=60)


def test_quota_concurrent_jobs_ingress(tmp_path: Path, monkeypatch) -> None:
    in_dir, _out_dir, _logs_dir = configure_runtime_paths(tmp_path)
    video_path = ensure_tiny_mp4(in_dir / "Test.mp4", skip_message="ffmpeg not available")
    monkeypatch.setenv("MAX_CONCURRENT_JOBS_PER_USER", "1")
    monkeypatch.setenv("JOBS_PER_DAY_PER_USER", "100")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10000000")
    monkeypatch.setenv("MAX_STORAGE_BYTES_PER_USER", "100000000")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("MIN_FREE_GB", "0")
    get_settings.cache_clear()

    with TestClient(app) as c:
        store = c.app.state.job_store
        auth = c.app.state.auth_store
        user = _make_user(username="quota_run_ingress", password="pass", role=Role.operator)
        auth.upsert_user(user)
        headers = login_user(c, username="quota_run_ingress", password="pass", clear_cookies=True)

        running = Job(
            id="job_running_quota_ingress",
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

        payload = {
            "video_path": str(video_path),
            "device": "cpu",
            "mode": "low",
            "series_title": "Series Q",
            "season_number": 1,
            "episode_number": 2,
        }
        resp = c.post("/api/jobs", headers=headers, json=payload)
        _assert_quota_response(resp, code="concurrent_jobs_limit", limit=1)
