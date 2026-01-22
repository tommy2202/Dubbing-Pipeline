from __future__ import annotations

import os
from pathlib import Path

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


def _login(c: TestClient, *, username: str, password: str) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def test_preview_endpoints_access(tmp_path: Path) -> None:
    _in_dir, out_dir, _logs_dir = _runtime_paths(tmp_path)
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["ENABLE_AUDIO_PREVIEW"] = "1"
    os.environ["ENABLE_LOWRES_PREVIEW"] = "1"
    get_settings.cache_clear()

    with TestClient(app) as c:
        store = c.app.state.job_store
        auth = c.app.state.auth_store
        ph = PasswordHasher()

        user_a = User(
            id=random_id("u_", 16),
            username="user_a",
            password_hash=ph.hash("pass_a"),
            role=Role.operator,
            totp_secret=None,
            totp_enabled=False,
            created_at=now_ts(),
        )
        user_b = User(
            id=random_id("u_", 16),
            username="user_b",
            password_hash=ph.hash("pass_b"),
            role=Role.viewer,
            totp_secret=None,
            totp_enabled=False,
            created_at=now_ts(),
        )
        auth.upsert_user(user_a)
        auth.upsert_user(user_b)

        headers_a = _login(c, username="user_a", password="pass_a")
        headers_b = _login(c, username="user_b", password="pass_b")
        headers_admin = _login(c, username="admin", password="adminpass")

        job_id = "job_preview_1"
        job_dir = out_dir / "job_preview_1"
        job_dir.mkdir(parents=True, exist_ok=True)
        preview_dir = job_dir / "preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        (preview_dir / "audio_preview.m4a").write_bytes(b"\x00" * 16)
        (preview_dir / "preview_lowres.mp4").write_bytes(b"\x00" * 16)

        job = Job(
            id=job_id,
            owner_id=user_a.id,
            video_path=str(_in_dir / "source.mp4"),
            duration_s=10.0,
            mode="low",
            device="cpu",
            src_lang="ja",
            tgt_lang="en",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            state=JobState.DONE,
            progress=1.0,
            message="done",
            output_mkv=str(job_dir / "job_preview_1.dub.mkv"),
            output_srt="",
            work_dir=str(job_dir),
            log_path=str(job_dir / "job.log"),
        )
        store.put(job)

        r_audio = c.get(
            f"/api/jobs/{job_id}/preview/audio",
            headers={**headers_a, "Range": "bytes=0-1"},
        )
        assert r_audio.status_code == 206

        r_low = c.get(
            f"/api/jobs/{job_id}/preview/lowres",
            headers={**headers_a, "Range": "bytes=0-1"},
        )
        assert r_low.status_code == 206

        r_b = c.get(
            f"/api/jobs/{job_id}/preview/audio",
            headers=headers_b,
        )
        assert r_b.status_code == 403

        r_admin = c.get(
            f"/api/jobs/{job_id}/preview/lowres",
            headers={**headers_admin, "Range": "bytes=0-1"},
        )
        assert r_admin.status_code == 206
