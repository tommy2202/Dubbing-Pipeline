from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from dubbing_pipeline.api.models import Role, User, now_ts
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState
from dubbing_pipeline.server import app
from dubbing_pipeline.utils.crypto import PasswordHasher, random_id


def _login(c: TestClient, *, username: str, password: str) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def _create_user(store, *, username: str, password: str, role: Role) -> User:
    ph = PasswordHasher()
    u = User(
        id=random_id("u_", 16),
        username=username,
        password_hash=ph.hash(password),
        role=role,
        totp_secret=None,
        totp_enabled=False,
        created_at=now_ts(),
    )
    store.upsert_user(u)
    return u


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
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
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "adminpass"
        os.environ["COOKIE_SECURE"] = "0"
        os.environ["MAX_QUEUE_DEPTH_GLOBAL"] = "1"
        os.environ["MAX_RUNNING_JOBS_PER_USER"] = "1"

        get_settings.cache_clear()

        video_path = in_dir / "Test.mp4"
        video_path.write_bytes(b"\x00" * 1024)

        with TestClient(app) as c:
            headers_admin = _login(c, username="admin", password="adminpass")
            store = c.app.state.job_store
            now = "2026-01-01T00:00:00+00:00"
            store.put(
                Job(
                    id="j_q_1",
                    owner_id="u1",
                    video_path=str(video_path),
                    duration_s=1.0,
                    mode="low",
                    device="cpu",
                    src_lang="ja",
                    tgt_lang="en",
                    created_at=now,
                    updated_at=now,
                    state=JobState.QUEUED,
                    progress=0.0,
                    message="Queued",
                    output_mkv="",
                    output_srt="",
                    work_dir="",
                    log_path=str(logs_dir / "job.log"),
                )
            )
            r = c.post(
                "/api/jobs",
                json={
                    "video_path": "Input/Test.mp4",
                    "series_title": "Show",
                    "series_slug": "show",
                    "season_number": 1,
                    "episode_number": 1,
                },
                headers=headers_admin,
            )
            assert r.status_code == 429

            auth_store = c.app.state.auth_store
            user = _create_user(auth_store, username="user1", password="pass1", role=Role.operator)
            headers_user = _login(c, username="user1", password="pass1")
            store.put(
                Job(
                    id="j_run_1",
                    owner_id=user.id,
                    video_path=str(video_path),
                    duration_s=1.0,
                    mode="low",
                    device="cpu",
                    src_lang="ja",
                    tgt_lang="en",
                    created_at=now,
                    updated_at=now,
                    state=JobState.RUNNING,
                    progress=0.1,
                    message="Running",
                    output_mkv="",
                    output_srt="",
                    work_dir="",
                    log_path=str(logs_dir / "job.log"),
                )
            )
            r2 = c.post(
                "/api/jobs",
                json={
                    "video_path": "Input/Test.mp4",
                    "series_title": "Show",
                    "series_slug": "show",
                    "season_number": 1,
                    "episode_number": 2,
                },
                headers=headers_user,
            )
            assert r2.status_code == 429

            os.environ["MIN_FREE_DISK_BYTES"] = "1000"
            get_settings.cache_clear()
            with patch("dubbing_pipeline.ops.storage.shutil.disk_usage") as mock_usage:
                mock_usage.return_value = type(
                    "du", (), {"total": 1000, "used": 1000, "free": 0}
                )()
                r3 = c.post(
                    "/api/uploads/init",
                    json={"filename": "clip.mp4", "total_bytes": 8, "mime": "video/mp4"},
                    headers=headers_admin,
                )
                assert r3.status_code == 507

        print("verify_limits: OK")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
