from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.api.models import Role, User, now_ts
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState, Visibility
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


def test_object_level_access_controls(tmp_path: Path) -> None:
    in_dir, out_dir, _logs_dir = _runtime_paths(tmp_path)
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    # seed input + output artifacts
    video_path = in_dir / "Test.mp4"
    video_path.write_bytes(b"\x00" * 1024)
    job_dir = out_dir / "Test"
    job_dir.mkdir(parents=True, exist_ok=True)
    output_file = job_dir / "dub.mp4"
    output_file.write_bytes(b"fake")
    log_path = job_dir / "job.log"
    log_path.write_text("hello\n", encoding="utf-8")

    with TestClient(app) as c:
        store = c.app.state.auth_store
        user_a = _create_user(store, username="user_a", password="pass_a", role=Role.operator)
        user_b = _create_user(store, username="user_b", password="pass_b", role=Role.operator)

        headers_a = _login(c, username="user_a", password="pass_a")
        headers_b = _login(c, username="user_b", password="pass_b")
        headers_admin = _login(c, username="admin", password="adminpass")

        # User A upload init
        r_up = c.post(
            "/api/uploads/init",
            json={"filename": "Test.mp4", "total_bytes": 1024, "mime": "video/mp4"},
            headers=headers_a,
        )
        assert r_up.status_code == 200, r_up.text
        upload_id = r_up.json()["upload_id"]

        # Create a job owned by User A
        now = "2026-01-01T00:00:00+00:00"
        c.app.state.job_store.put(
            Job(
                id="j_obj_1",
                owner_id=user_a.id,
                video_path=str(video_path),
                duration_s=1.0,
                mode="low",
                device="cpu",
                src_lang="ja",
                tgt_lang="en",
                created_at=now,
                updated_at=now,
                state=JobState.DONE,
                progress=1.0,
                message="Done",
                output_mkv=str(output_file),
                output_srt="",
                work_dir=str(job_dir),
                log_path=str(log_path),
                series_title="Show A",
                series_slug="show-a",
                season_number=1,
                episode_number=1,
                visibility=Visibility.private,
            )
        )

        # User B cannot access User A job details/logs/artifacts
        assert c.get("/api/jobs/j_obj_1", headers=headers_b).status_code == 403
        assert c.get("/api/jobs/j_obj_1/logs/tail", headers=headers_b).status_code == 403
        assert c.get("/api/jobs/j_obj_1/files", headers=headers_b).status_code == 403

        rel_path = f"{job_dir.name}/{output_file.name}"
        assert c.get(f"/files/{rel_path}", headers=headers_b).status_code == 403

        # User B cannot access User A uploads
        assert c.get(f"/api/uploads/{upload_id}", headers=headers_b).status_code == 403

        # Library items should not leak
        r_lib = c.get("/api/library/series", headers=headers_b)
        assert r_lib.status_code == 200
        items = r_lib.json()
        assert all(it.get("series_slug") != "show-a" for it in items)

        # Admin can access
        assert c.get("/api/jobs/j_obj_1", headers=headers_admin).status_code == 200
        assert c.get("/api/jobs/j_obj_1/logs/tail", headers=headers_admin).status_code == 200
        assert c.get("/api/uploads/" + upload_id, headers=headers_admin).status_code == 200

        r_files = c.get("/api/jobs/j_obj_1/files", headers=headers_admin)
        assert r_files.status_code == 200
        file_url = r_files.json()["mp4"]["url"]
        assert c.get(file_url, headers=headers_admin).status_code == 206
