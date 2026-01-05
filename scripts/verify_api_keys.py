from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient


def _login_admin(c: TestClient) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass", "session": True})
    assert r.status_code == 200
    d = r.json()
    return {"X-CSRF-Token": d["csrf_token"]}


def main() -> int:
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(Path("/tmp") / "anime_v2_keys_out")
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"

    from anime_v2.api.models import Role, User
    from anime_v2.config import get_settings
    from anime_v2.server import app
    from anime_v2.utils.crypto import PasswordHasher

    get_settings.cache_clear()

    with TestClient(app) as c:
        store = c.app.state.auth_store
        ph = PasswordHasher()
        store.upsert_user(
            User(
                id="u_view_k",
                username="viewer_k",
                password_hash=ph.hash("pass"),
                role=Role.viewer,
                totp_secret=None,
                totp_enabled=False,
                created_at=1,
            )
        )
        store.upsert_user(
            User(
                id="u_op_k",
                username="operator_k",
                password_hash=ph.hash("pass"),
                role=Role.operator,
                totp_secret=None,
                totp_enabled=False,
                created_at=1,
            )
        )
        store.upsert_user(
            User(
                id="u_ed_k",
                username="editor_k",
                password_hash=ph.hash("pass"),
                role=Role.editor,
                totp_secret=None,
                totp_enabled=False,
                created_at=1,
            )
        )

    # Dummy job for edit tests
    from anime_v2.jobs.models import Job, JobState

    out_root = Path(os.environ["ANIME_V2_OUTPUT_DIR"])
    job_dir = out_root / "Sample"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "review").mkdir(parents=True, exist_ok=True)

    with TestClient(app) as c:
        c.app.state.job_store.put(
            Job(
                id="j_keys_1",
                owner_id="u_ed_k",
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

    with TestClient(app) as c_admin:
        hdr = _login_admin(c_admin)
        store = c_admin.app.state.auth_store
        admin_u = store.get_user_by_username("admin")
        assert admin_u is not None
        # Create scoped keys for each role user
        k_view = c_admin.post("/keys", json={"user_id": "u_view_k", "scopes": ["read:job"]}, headers=hdr).json()["key"]
        k_submit = c_admin.post("/keys", json={"user_id": "u_op_k", "scopes": ["submit:job"]}, headers=hdr).json()["key"]
        k_edit = c_admin.post("/keys", json={"user_id": "u_ed_k", "scopes": ["edit:job"]}, headers=hdr).json()["key"]
        k_admin = c_admin.post("/keys", json={"user_id": admin_u.id, "scopes": ["admin:*"]}, headers=hdr)
        assert k_admin.status_code == 200
        k_admin_val = k_admin.json()["key"]

    # View-only key
    with TestClient(app) as c:
        assert c.get("/api/jobs?limit=1", headers={"X-Api-Key": k_view}).status_code == 200
        assert (
            c.post("/api/uploads/init", json={"filename": "x.mp4", "total_bytes": 1}, headers={"X-Api-Key": k_view}).status_code
            == 403
        )

    # Submit-only key
    with TestClient(app) as c:
        assert (
            c.post("/api/uploads/init", json={"filename": "x.mp4", "total_bytes": 1}, headers={"X-Api-Key": k_submit}).status_code
            == 200
        )
        assert c.get("/api/jobs?limit=1", headers={"X-Api-Key": k_submit}).status_code == 403

    # Edit-only key
    with TestClient(app) as c:
        assert (
            c.put(
                "/api/jobs/j_keys_1/overrides",
                json={"speaker_overrides": {"1": "SPEAKER_01"}},
                headers={"X-Api-Key": k_edit},
            ).status_code
            == 200
        )
        assert (
            c.post("/api/uploads/init", json={"filename": "x.mp4", "total_bytes": 1}, headers={"X-Api-Key": k_edit}).status_code
            == 403
        )

    # Admin key can manage keys
    with TestClient(app) as c:
        assert c.get("/keys", headers={"X-Api-Key": k_admin_val}).status_code == 200

    print("verify_api_keys: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

