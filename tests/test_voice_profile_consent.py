from __future__ import annotations

import os
from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.server import app


def _login_admin(c: TestClient) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    assert r.status_code == 200
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def test_voice_profile_consent_policy_blocks() -> None:
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        headers = _login_admin(c)
        store = c.app.state.job_store
        auth_store = c.app.state.auth_store
        admin_user = auth_store.get_user_by_username("admin")
        admin_id = str(admin_user.id if admin_user else "admin")

        # Extracted media: cannot enable sharing/export/reuse.
        store.upsert_voice_profile(
            profile_id="vp_extract",
            display_name="Extracted",
            created_by=admin_id,
            scope="private",
            series_lock="series-a",
            source_type="extracted_from_media",
            export_allowed=False,
            share_allowed=False,
            reuse_allowed=0,
            expires_at=None,
            embedding_vector=None,
            embedding_model_id="",
            metadata_json={},
        )
        r = c.post(
            "/api/voices/vp_extract/consent",
            headers=headers,
            json={
                "source_type": "extracted_from_media",
                "scope": "private",
                "share_allowed": True,
                "export_allowed": False,
                "reuse_allowed": False,
            },
        )
        assert r.status_code == 422

        # Unknown cannot enable reuse or global scope.
        store.upsert_voice_profile(
            profile_id="vp_unknown",
            display_name="Unknown",
            created_by=admin_id,
            scope="private",
            series_lock="",
            source_type="unknown",
            export_allowed=False,
            share_allowed=False,
            reuse_allowed=0,
            expires_at=None,
            embedding_vector=None,
            embedding_model_id="",
            metadata_json={},
        )
        r2 = c.post(
            "/api/voices/vp_unknown/consent",
            headers=headers,
            json={
                "source_type": "unknown",
                "scope": "global",
                "share_allowed": True,
                "export_allowed": True,
                "reuse_allowed": True,
            },
        )
        assert r2.status_code == 422


def test_voice_profile_consent_user_upload_allows_global() -> None:
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        headers = _login_admin(c)
        store = c.app.state.job_store
        auth_store = c.app.state.auth_store
        admin_user = auth_store.get_user_by_username("admin")
        admin_id = str(admin_user.id if admin_user else "admin")

        store.upsert_voice_profile(
            profile_id="vp_upload",
            display_name="Upload",
            created_by=admin_id,
            scope="private",
            series_lock="",
            source_type="user_upload",
            export_allowed=False,
            share_allowed=False,
            reuse_allowed=0,
            expires_at=None,
            embedding_vector=None,
            embedding_model_id="",
            metadata_json={},
        )
        r = c.post(
            "/api/voices/vp_upload/consent",
            headers=headers,
            json={
                "source_type": "user_upload",
                "scope": "global",
                "share_allowed": True,
                "export_allowed": True,
                "reuse_allowed": True,
            },
        )
        assert r.status_code == 200
        prof = store.get_voice_profile("vp_upload")
        assert prof is not None
        assert prof["scope"] == "global"
        assert bool(prof["reuse_allowed"]) is True
