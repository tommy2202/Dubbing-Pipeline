from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dubbing_pipeline.api.models import Role, User, now_ts
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState, Visibility, now_utc
from dubbing_pipeline.library.paths import get_job_output_root, get_library_root_for_job
from dubbing_pipeline.ops.storage import job_storage_bytes
from dubbing_pipeline.server import app
from dubbing_pipeline.utils.crypto import PasswordHasher, random_id
from tests._helpers.auth import login_user


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


def _seed_job_storage(*, job: Job, base_bytes: int, library_bytes: int) -> int:
    base_dir = get_job_output_root(job)
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / "artifact.bin").write_bytes(b"a" * int(base_bytes))
    lib_dir = get_library_root_for_job(job)
    lib_dir.mkdir(parents=True, exist_ok=True)
    (lib_dir / "artifact.bin").write_bytes(b"b" * int(library_bytes))
    return int(job_storage_bytes(job=job))


def test_storage_quota_blocks_upload(tmp_path: Path) -> None:
    in_dir, _out_dir, _logs_dir = _runtime_paths(tmp_path)
    os.environ["MAX_STORAGE_BYTES_PER_USER"] = "800"
    os.environ["MAX_UPLOAD_BYTES"] = "10000000"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["MIN_FREE_GB"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        auth = c.app.state.auth_store
        store = c.app.state.job_store
        user = _make_user(username="storage_upload", password="pass", role=Role.operator)
        auth.upsert_user(user)
        headers = login_user(c, username="storage_upload", password="pass", clear_cookies=True)

        (in_dir / "Test.mp4").write_bytes(b"\x00")
        job = Job(
            id="job_storage_upload",
            owner_id=user.id,
            video_path=str(in_dir / "Test.mp4"),
            duration_s=1.0,
            mode="low",
            device="cpu",
            src_lang="ja",
            tgt_lang="en",
            created_at=now_utc(),
            updated_at=now_utc(),
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
            visibility=Visibility.private,
        )
        used = _seed_job_storage(job=job, base_bytes=300, library_bytes=400)
        store.set_job_storage_bytes(job.id, user_id=user.id, bytes_count=used)

        r = c.post(
            "/api/uploads/init",
            headers=headers,
            json={"filename": "clip.mp4", "total_bytes": 150},
        )
        assert r.status_code == 429, r.text
        detail = r.json()
        assert detail.get("code") == "storage_bytes_limit"


def test_storage_quota_blocks_job_submit(tmp_path: Path) -> None:
    video_path = _runtime_video_path(tmp_path)
    os.environ["MAX_STORAGE_BYTES_PER_USER"] = "450"
    os.environ["MAX_UPLOAD_BYTES"] = "10000000"
    os.environ["JOBS_PER_DAY_PER_USER"] = "100000"
    os.environ["MAX_CONCURRENT_JOBS_PER_USER"] = "100000"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["MIN_FREE_GB"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        auth = c.app.state.auth_store
        store = c.app.state.job_store
        user = _make_user(username="storage_submit", password="pass", role=Role.operator)
        auth.upsert_user(user)
        headers = login_user(c, username="storage_submit", password="pass", clear_cookies=True)

        job = Job(
            id="job_storage_submit",
            owner_id=user.id,
            video_path=str(video_path),
            duration_s=1.0,
            mode="low",
            device="cpu",
            src_lang="ja",
            tgt_lang="en",
            created_at=now_utc(),
            updated_at=now_utc(),
            state=JobState.DONE,
            progress=1.0,
            message="done",
            output_mkv="",
            output_srt="",
            work_dir="",
            log_path="",
            error=None,
            series_title="Series T",
            series_slug="series-t",
            season_number=1,
            episode_number=1,
            visibility=Visibility.private,
        )
        used = _seed_job_storage(job=job, base_bytes=200, library_bytes=250)
        store.set_job_storage_bytes(job.id, user_id=user.id, bytes_count=used)

        payload = {
            "video_path": str(video_path),
            "device": "cpu",
            "mode": "low",
            "series_title": "Series T",
            "season_number": 1,
            "episode_number": 2,
        }
        r = c.post("/api/jobs", headers=headers, json=payload)
        assert r.status_code == 429, r.text
        detail = r.json()
        assert detail.get("code") == "storage_bytes_limit"
