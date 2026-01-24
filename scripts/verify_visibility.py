from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient


def _login(c: TestClient, *, username: str, password: str) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    data = r.json()
    headers = {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}
    # Clear cookies so CSRF is not enforced for bearer-token API calls.
    c.cookies.clear()
    return headers


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        out_root = (root / "Output").resolve()
        in_root = (root / "Input").resolve()
        logs_root = (root / "logs").resolve()
        out_root.mkdir(parents=True, exist_ok=True)
        in_root.mkdir(parents=True, exist_ok=True)
        logs_root.mkdir(parents=True, exist_ok=True)

        os.environ["APP_ROOT"] = str(root)
        os.environ["INPUT_DIR"] = str(in_root)
        os.environ["DUBBING_OUTPUT_DIR"] = str(out_root)
        os.environ["DUBBING_LOG_DIR"] = str(logs_root)
        os.environ["DUBBING_STATE_DIR"] = str(root / "_state")
        os.environ["MIN_FREE_GB"] = "0"
        os.environ["DUBBING_SKIP_STARTUP_CHECK"] = "1"
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "adminpass"
        os.environ["COOKIE_SECURE"] = "0"

        from dubbing_pipeline.api.models import Role, User, now_ts
        from dubbing_pipeline.config import get_settings
        from dubbing_pipeline.jobs.models import Job, JobState, Visibility
        from dubbing_pipeline.server import app
        from dubbing_pipeline.utils.crypto import PasswordHasher, random_id

        get_settings.cache_clear()

        with TestClient(app) as c:
            store = c.app.state.job_store
            auth = c.app.state.auth_store
            ph = PasswordHasher()

            owner = User(
                id=random_id("u_", 16),
                username="owner_v",
                password_hash=ph.hash("pass_owner"),
                role=Role.operator,
                totp_secret=None,
                totp_enabled=False,
                created_at=now_ts(),
            )
            other = User(
                id=random_id("u_", 16),
                username="other_v",
                password_hash=ph.hash("pass_other"),
                role=Role.viewer,
                totp_secret=None,
                totp_enabled=False,
                created_at=now_ts(),
            )
            auth.upsert_user(owner)
            auth.upsert_user(other)

            headers_owner = _login(c, username="owner_v", password="pass_owner")
            headers_other = _login(c, username="other_v", password="pass_other")
            headers_admin = _login(c, username="admin", password="adminpass")

            job_id = "job_visibility_v"
            job_dir = out_root / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            output_mkv = job_dir / f"{job_id}.dub.mp4"
            output_mkv.write_bytes(b"\x00" * 16)

            job = Job(
                id=job_id,
                owner_id=owner.id,
                video_path=str(in_root / "source.mp4"),
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
                log_path=str(job_dir / "job.log"),
                series_title="Series V",
                series_slug="series-v",
                season_number=1,
                episode_number=1,
            )
            assert job.visibility == Visibility.private
            store.put(job)

            # Private: other cannot see library or files.
            assert (
                c.get("/api/library/series-v/seasons", headers=headers_other).status_code == 403
            )
            rel = output_mkv.relative_to(out_root).as_posix()
            assert c.get(f"/files/{rel}", headers=headers_other).status_code == 403

            # Owner shares
            r = c.post(
                f"/api/jobs/{job_id}/visibility",
                headers=headers_owner,
                json={"visibility": "shared"},
            )
            assert r.status_code == 200

            # Shared: other can see library and files.
            assert c.get("/api/library/series-v/seasons", headers=headers_other).status_code == 200
            assert c.get(f"/files/{rel}", headers=headers_other).status_code in {200, 206}

            # Admin can set back to private.
            r = c.post(
                f"/api/jobs/{job_id}/visibility",
                headers=headers_admin,
                json={"visibility": "private"},
            )
            assert r.status_code == 200
            assert (
                c.get("/api/library/series-v/seasons", headers=headers_other).status_code == 403
            )

        print("verify_visibility: ok")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
