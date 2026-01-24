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
                username="owner_r",
                password_hash=ph.hash("pass_owner"),
                role=Role.operator,
                totp_secret=None,
                totp_enabled=False,
                created_at=now_ts(),
            )
            other = User(
                id=random_id("u_", 16),
                username="other_r",
                password_hash=ph.hash("pass_other"),
                role=Role.viewer,
                totp_secret=None,
                totp_enabled=False,
                created_at=now_ts(),
            )
            auth.upsert_user(owner)
            auth.upsert_user(other)

            headers_owner = _login(c, username="owner_r", password="pass_owner")
            headers_other = _login(c, username="other_r", password="pass_other")
            headers_admin = _login(c, username="admin", password="adminpass")

            job_id = "job_report_1"
            job = Job(
                id=job_id,
                owner_id=owner.id,
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
                series_title="Series R",
                series_slug="series-r",
                season_number=1,
                episode_number=1,
                visibility=Visibility.shared,
            )
            store.put(job)

            key = "series-r:1:1"

            report = c.post(
                f"/api/library/{key}/report",
                headers=headers_other,
                json={"reason": "Inappropriate content"},
            )
            assert report.status_code == 200, report.text
            report_id = report.json().get("report_id")
            assert report_id

            admin_reports = c.get("/api/admin/reports?status=open", headers=headers_admin)
            assert admin_reports.status_code == 200, admin_reports.text
            items = admin_reports.json().get("items") or []
            assert any(it.get("id") == report_id for it in items)

            unshare = c.post(f"/api/library/{key}/unshare", headers=headers_owner, json={})
            assert unshare.status_code == 200, unshare.text
            job2 = store.get(job_id)
            assert job2 is not None
            assert job2.visibility == Visibility.private

            store.update(job_id, visibility="shared")
            admin_remove = c.post(f"/api/library/{key}/admin_remove", headers=headers_admin, json={})
            assert admin_remove.status_code == 200, admin_remove.text
            con = store._conn()
            try:
                row = con.execute("SELECT COUNT(*) AS cnt FROM job_library WHERE job_id = ?;", (job_id,)).fetchone()
                assert row is not None
                assert int(row["cnt"] or 0) == 0
            finally:
                con.close()

        print("verify_reports: ok")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
