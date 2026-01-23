from __future__ import annotations

import os
import tempfile
from pathlib import Path

try:
    from fastapi.testclient import TestClient
except Exception as ex:  # pragma: no cover - optional deps
    print(f"verify_voice_versioning: SKIP (fastapi unavailable: {ex})")
    raise SystemExit(0)

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
            auth.upsert_user(owner)
            headers_owner = _login(c, username="owner", password="ownerpass")

            job = Job(
                id="job_voice_versions_verify",
                owner_id=owner.id,
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
                output_mkv=str(out_dir / "job_voice_versions_verify" / "out.mkv"),
                output_srt="",
                work_dir=str(out_dir / "job_voice_versions_verify"),
                log_path=str(out_dir / "job_voice_versions_verify" / "job.log"),
                series_title="Series V",
                series_slug="series-v",
                season_number=1,
                episode_number=1,
                visibility=Visibility.private,
            )
            store.put(job)

            r = c.post(
                "/api/series/series-v/characters",
                headers=headers_owner,
                json={"display_name": "Nova"},
            )
            assert r.status_code == 200, r.text
            cslug = r.json()["character"]["character_slug"]

            r = c.post(
                f"/api/series/series-v/characters/{cslug}/ref",
                headers=headers_owner,
                data=b"\x00" * 32,
            )
            assert r.status_code == 200, r.text

            r = c.get(f"/api/voices/series-v/{cslug}/versions", headers=headers_owner)
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["items"] and data["items"][0]["version"] == 1

            r = c.post(
                f"/api/voices/series-v/{cslug}/rollback?version=1",
                headers=headers_owner,
            )
            assert r.status_code == 200, r.text

            r = c.get(f"/api/voices/series-v/{cslug}/versions", headers=headers_owner)
            assert r.status_code == 200, r.text
            data = r.json()
            assert len(data["items"]) >= 2

    print("verify_voice_versioning: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
