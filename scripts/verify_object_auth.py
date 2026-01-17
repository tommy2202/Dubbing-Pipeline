from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.api.models import Role, User, now_ts
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState, Visibility
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

        get_settings.cache_clear()

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
            _ = _create_user(store, username="user_b", password="pass_b", role=Role.operator)

            headers_a = _login(c, username="user_a", password="pass_a")
            headers_b = _login(c, username="user_b", password="pass_b")
            headers_admin = _login(c, username="admin", password="adminpass")

            r_up = c.post(
                "/api/uploads/init",
                json={"filename": "Test.mp4", "total_bytes": 1024, "mime": "video/mp4"},
                headers=headers_a,
            )
            assert r_up.status_code == 200, r_up.text
            upload_id = r_up.json()["upload_id"]

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

            assert c.get("/api/jobs/j_obj_1", headers=headers_b).status_code == 403
            assert c.get("/api/jobs/j_obj_1/logs/tail", headers=headers_b).status_code == 403
            assert c.get("/api/jobs/j_obj_1/files", headers=headers_b).status_code == 403
            assert c.get(f"/api/uploads/{upload_id}", headers=headers_b).status_code == 403

            rel_path = f"{job_dir.name}/{output_file.name}"
            assert c.get(f"/files/{rel_path}", headers=headers_b).status_code == 403

            assert c.get("/api/jobs/j_obj_1", headers=headers_admin).status_code == 200
            assert c.get("/api/jobs/j_obj_1/logs/tail", headers=headers_admin).status_code == 200
            assert c.get(f"/api/uploads/{upload_id}", headers=headers_admin).status_code == 200

        print("verify_object_auth: OK")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
