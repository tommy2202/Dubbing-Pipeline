from __future__ import annotations

from fastapi.testclient import TestClient

from dubbing_pipeline.api.models import Role, User, now_ts
from dubbing_pipeline.jobs.models import Job, JobState, Visibility
from dubbing_pipeline.server import app
from dubbing_pipeline.utils.crypto import PasswordHasher, random_id


def _login(c: TestClient, *, username: str, password: str) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def test_voice_versioning_and_rollback() -> None:
    with TestClient(app) as c:
        store = c.app.state.job_store
        auth = c.app.state.auth_store
        ph = PasswordHasher()

        owner = User(
            id=random_id("u_", 16),
            username="owner",
            password_hash=ph.hash("ownerpass"),
            role=Role.operator,
            totp_secret=None,
            totp_enabled=False,
            created_at=now_ts(),
        )
        other = User(
            id=random_id("u_", 16),
            username="other",
            password_hash=ph.hash("otherpass"),
            role=Role.viewer,
            totp_secret=None,
            totp_enabled=False,
            created_at=now_ts(),
        )
        auth.upsert_user(owner)
        auth.upsert_user(other)

        headers_owner = _login(c, username="owner", password="ownerpass")
        headers_other = _login(c, username="other", password="otherpass")

        job = Job(
            id="job_voice_versions",
            owner_id=owner.id,
            video_path="/tmp/source.mp4",
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
            output_mkv="/tmp/out.mkv",
            output_srt="",
            work_dir="/tmp/job_voice_versions",
            log_path="/tmp/job_voice_versions/job.log",
            series_title="Series X",
            series_slug="series-x",
            season_number=1,
            episode_number=1,
            visibility=Visibility.private,
        )
        store.put(job)

        r = c.post(
            "/api/series/series-x/characters",
            headers=headers_owner,
            json={"display_name": "Hero"},
        )
        assert r.status_code == 200, r.text
        cslug = r.json()["character"]["character_slug"]

        r = c.post(
            f"/api/series/series-x/characters/{cslug}/ref",
            headers=headers_owner,
            data=b"\x00" * 32,
        )
        assert r.status_code == 200, r.text

        r = c.get(f"/api/voices/series-x/{cslug}/versions", headers=headers_owner)
        assert r.status_code == 200, r.text
        data = r.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["version"] == 1

        r = c.get(f"/api/voices/series-x/{cslug}/versions", headers=headers_other)
        assert r.status_code == 403, r.text

        r = c.post(
            f"/api/voices/series-x/{cslug}/rollback?version=1",
            headers=headers_owner,
        )
        assert r.status_code == 200, r.text

        r = c.get(f"/api/voices/series-x/{cslug}/versions", headers=headers_owner)
        assert r.status_code == 200, r.text
        data = r.json()
        assert len(data["items"]) >= 2
        assert int(data["current_version"]) >= 2
