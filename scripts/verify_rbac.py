from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient


def _login(c: TestClient, username: str, password: str) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": username, "password": password, "session": True})
    assert r.status_code == 200
    d = r.json()
    return {"csrf": d["csrf_token"]}


def main() -> int:
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(Path("/tmp") / "anime_v2_rbac_out")
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["ENABLE_QR_LOGIN"] = "0"

    from anime_v2.api.models import Role, User
    from anime_v2.config import get_settings
    from anime_v2.server import app
    from anime_v2.utils.crypto import PasswordHasher

    get_settings.cache_clear()

    with TestClient(app) as c:
        store = c.app.state.auth_store
        ph = PasswordHasher()

        # Create users
        store.upsert_user(
            User(
                id="u_view",
                username="viewer1",
                password_hash=ph.hash("pass"),
                role=Role.viewer,
                totp_secret=None,
                totp_enabled=False,
                created_at=1,
            )
        )
        store.upsert_user(
            User(
                id="u_op",
                username="operator1",
                password_hash=ph.hash("pass"),
                role=Role.operator,
                totp_secret=None,
                totp_enabled=False,
                created_at=1,
            )
        )
        store.upsert_user(
            User(
                id="u_ed",
                username="editor1",
                password_hash=ph.hash("pass"),
                role=Role.editor,
                totp_secret=None,
                totp_enabled=False,
                created_at=1,
            )
        )

    # Seed a dummy job so edit endpoints have a target.
    from anime_v2.jobs.models import Job, JobState

    out_root = Path(os.environ["ANIME_V2_OUTPUT_DIR"])
    job_dir = out_root / "Sample"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "review").mkdir(parents=True, exist_ok=True)

    with TestClient(app) as c:
        c.app.state.job_store.put(
            Job(
                id="j_rbac_1",
                owner_id="u_op",
                video_path=str(Path("/workspace/Input/Test.mp4")),
                duration_s=1.0,
                mode="low",
                device="cpu",
                src_lang="ja",
                tgt_lang="en",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                state=JobState.DONE,
                progress=1.0,
                message="Done",
                output_mkv="",
                output_srt="",
                work_dir=str(job_dir),
                log_path=str(job_dir / "job.log"),
            )
        )

    # viewer: can read jobs, cannot submit, cannot edit
    with TestClient(app) as c_view:
        csrf = _login(c_view, "viewer1", "pass")["csrf"]
        assert c_view.get("/api/jobs?limit=1").status_code == 200
        assert (
            c_view.post("/api/uploads/init", json={"filename": "x.mp4", "total_bytes": 1}).status_code == 403
        )
        assert (
            c_view.put(
                "/api/jobs/j_rbac_1/overrides",
                json={"speaker_overrides": {"1": "SPEAKER_01"}},
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 403
        )

    # operator: can submit, cannot edit
    with TestClient(app) as c_op:
        csrf = _login(c_op, "operator1", "pass")["csrf"]
        assert (
            c_op.post(
                "/api/uploads/init",
                json={"filename": "x.mp4", "total_bytes": 1},
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 200
        )
        assert (
            c_op.put(
                "/api/jobs/j_rbac_1/overrides",
                json={"speaker_overrides": {"1": "SPEAKER_01"}},
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 403
        )

    # editor: can edit, cannot submit
    with TestClient(app) as c_ed:
        csrf = _login(c_ed, "editor1", "pass")["csrf"]
        assert (
            c_ed.post("/api/uploads/init", json={"filename": "x.mp4", "total_bytes": 1}).status_code == 403
        )
        assert (
            c_ed.put(
                "/api/jobs/j_rbac_1/overrides",
                json={"speaker_overrides": {"1": "SPEAKER_01"}},
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 200
        )

    # admin: settings and keys are admin-only
    with TestClient(app) as c_admin:
        csrf = _login(c_admin, "admin", "adminpass")["csrf"]
        assert (
            c_admin.put("/api/settings", json={"defaults": {"mode": "low"}}, headers={"X-CSRF-Token": csrf}).status_code
            == 200
        )
        assert c_admin.get("/keys").status_code == 200

    print("verify_rbac: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

