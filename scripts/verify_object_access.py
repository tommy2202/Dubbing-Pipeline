from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient


def _login(c: TestClient, *, username: str, password: str) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def main() -> int:
    from dubbing_pipeline.api.models import Role, User, now_ts
    from dubbing_pipeline.config import get_settings
    from dubbing_pipeline.jobs.models import Job, JobState, Visibility
    from dubbing_pipeline.server import app
    from dubbing_pipeline.utils.crypto import PasswordHasher, random_id

    with tempfile.TemporaryDirectory(prefix="verify_object_access_") as td:
        root = Path(td)
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
        os.environ["MIN_FREE_GB"] = "0"

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

            job_id = "job_access_1"
            job_dir = out_dir / "job_access_1"
            job_dir.mkdir(parents=True, exist_ok=True)
            output_mkv = job_dir / "job_access_1.dub.mp4"
            output_mkv.write_bytes(b"\x00" * 16)
            log_path = job_dir / "job.log"
            log_path.write_text("hello\n", encoding="utf-8")

            job = Job(
                id=job_id,
                owner_id=user_a.id,
                video_path=str(in_dir / "source.mp4"),
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
                output_mkv=str(output_mkv),
                output_srt="",
                work_dir=str(job_dir),
                log_path=str(log_path),
                series_title="Series A",
                series_slug="series-a",
                season_number=1,
                episode_number=1,
                visibility=Visibility.private,
            )
            store.put(job)

            upload_id = "up_access_1"
            store.put_upload(
                upload_id,
                {
                    "id": upload_id,
                    "owner_id": user_a.id,
                    "filename": "x.mp4",
                    "total_bytes": 10,
                    "chunk_bytes": 5,
                    "received": {},
                    "received_bytes": 0,
                    "completed": True,
                    "final_path": str(in_dir / "uploads" / "x.mp4"),
                },
            )

            assert c.get(f"/api/jobs/{job_id}", headers=headers_a).status_code == 200
            assert c.get(f"/api/jobs/{job_id}", headers=headers_b).status_code == 403
            assert c.get(f"/api/jobs/{job_id}", headers=headers_admin).status_code == 200

            assert c.get(f"/api/uploads/{upload_id}", headers=headers_a).status_code == 200
            assert c.get(f"/api/uploads/{upload_id}", headers=headers_b).status_code == 403
            assert c.get(f"/api/uploads/{upload_id}", headers=headers_admin).status_code == 200

            assert c.get("/api/library/series-a/seasons", headers=headers_a).status_code == 200
            assert c.get("/api/library/series-a/seasons", headers=headers_b).status_code == 403
            assert c.get("/api/library/series-a/seasons", headers=headers_admin).status_code == 200

            rel = output_mkv.relative_to(out_dir).as_posix()
            assert c.get(f"/files/{rel}", headers=headers_a).status_code in {200, 206}
            assert c.get(f"/files/{rel}", headers=headers_b).status_code == 403
            assert c.get(f"/files/{rel}", headers=headers_admin).status_code in {200, 206}

        print("verify_object_access: PASS")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
