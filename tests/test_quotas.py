from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dubbing_pipeline.api.models import Role, User, now_ts
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState
from dubbing_pipeline.server import app
from dubbing_pipeline.utils.crypto import PasswordHasher, random_id


def _runtime_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path.resolve()
    in_dir = root / "Input"
    out_dir = root / "Output"
    logs_dir = root / "logs"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    os.environ["APP_ROOT"] = str(root)
    os.environ["INPUT_DIR"] = str(in_dir)
    os.environ["DUBBING_OUTPUT_DIR"] = str(out_dir)
    os.environ["DUBBING_LOG_DIR"] = str(logs_dir)
    return in_dir, out_dir, logs_dir


def _runtime_video_path(tmp_path: Path) -> Path:
    root = tmp_path.resolve()
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")
    in_dir, _out_dir, _logs_dir = _runtime_paths(tmp_path)
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


def _login(c: TestClient, *, username: str, password: str) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    data = r.json()
    headers = {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}
    c.cookies.clear()
    return headers


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


def test_upload_quota_limits(tmp_path: Path) -> None:
    _runtime_paths(tmp_path)
    os.environ["MAX_UPLOAD_BYTES"] = "1024"
    os.environ["MAX_STORAGE_BYTES_PER_USER"] = "2048"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["MIN_FREE_GB"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        auth = c.app.state.auth_store
        store = c.app.state.job_store
        user = _make_user(username="quota_user", password="pass", role=Role.operator)
        auth.upsert_user(user)
        headers = _login(c, username="quota_user", password="pass")

        store.set_job_storage_bytes("job_storage_1", user_id=user.id, bytes_count=1900)
        r = c.post(
            "/api/uploads/init",
            headers=headers,
            json={"filename": "clip.mp4", "total_bytes": 400},
        )
        assert r.status_code == 429, r.text

        # Clear storage usage; then enforce max upload bytes.
        store.set_job_storage_bytes("job_storage_1", user_id=user.id, bytes_count=0)
        r2 = c.post(
            "/api/uploads/init",
            headers=headers,
            json={"filename": "clip.mp4", "total_bytes": 2048},
        )
        assert r2.status_code == 400, r2.text


def test_jobs_per_day_cap(tmp_path: Path) -> None:
    video_path = _runtime_video_path(tmp_path)
    os.environ["JOBS_PER_DAY_PER_USER"] = "1"
    os.environ["MAX_UPLOAD_BYTES"] = "10000000"
    os.environ["MAX_STORAGE_BYTES_PER_USER"] = "100000000"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["MIN_FREE_GB"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        auth = c.app.state.auth_store
        user = _make_user(username="quota_daily", password="pass", role=Role.operator)
        auth.upsert_user(user)
        headers = _login(c, username="quota_daily", password="pass")

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


def test_concurrent_job_cap_before_run(tmp_path: Path) -> None:
    _runtime_paths(tmp_path)
    os.environ["MAX_CONCURRENT_JOBS_PER_USER"] = "1"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["MIN_FREE_GB"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        store = c.app.state.job_store
        auth = c.app.state.auth_store
        user = _make_user(username="quota_run", password="pass", role=Role.operator)
        auth.upsert_user(user)

        running = Job(
            id="job_running",
            owner_id=user.id,
            video_path="",
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
        queued = Job(
            id="job_queued",
            owner_id=user.id,
            video_path="",
            duration_s=1.0,
            mode="low",
            device="cpu",
            src_lang="ja",
            tgt_lang="en",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            state=JobState.QUEUED,
            progress=0.0,
            message="queued",
            output_mkv="",
            output_srt="",
            work_dir="",
            log_path="",
            error=None,
            series_title="Series R",
            series_slug="series-r",
            season_number=1,
            episode_number=2,
        )
        store.put(running)
        store.put(queued)

        qb = c.app.state.queue_backend
        ok = asyncio.run(qb.before_job_run(job_id=queued.id, user_id=str(user.id)))
        assert ok is False


def test_storage_accounting_delete_job(tmp_path: Path) -> None:
    _runtime_paths(tmp_path)
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["MIN_FREE_GB"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        store = c.app.state.job_store
        auth = c.app.state.auth_store
        user = _make_user(username="quota_storage", password="pass", role=Role.operator)
        auth.upsert_user(user)

        job = Job(
            id="job_storage",
            owner_id=user.id,
            video_path="",
            duration_s=1.0,
            mode="low",
            device="cpu",
            src_lang="ja",
            tgt_lang="en",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            state=JobState.DONE,
            progress=1.0,
            message="done",
            output_mkv="",
            output_srt="",
            work_dir="",
            log_path="",
            error=None,
            series_title="Series S",
            series_slug="series-s",
            season_number=1,
            episode_number=1,
        )
        store.put(job)
        store.set_job_storage_bytes(job.id, user_id=user.id, bytes_count=1234)
        assert store.get_user_storage_bytes(user.id) == 1234
        store.delete_job(job.id)
        assert store.get_user_storage_bytes(user.id) == 0
