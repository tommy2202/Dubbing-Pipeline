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


def _job(
    *,
    job_id: str,
    owner_id: str,
    series_title: str,
    series_slug: str,
    season_number: int,
    episode_number: int,
    created_at: str,
) -> Job:
    return Job(
        id=job_id,
        owner_id=owner_id,
        video_path="/tmp/source.mp4",
        duration_s=1.0,
        mode="low",
        device="cpu",
        src_lang="ja",
        tgt_lang="en",
        created_at=created_at,
        updated_at=created_at,
        state=JobState.DONE,
        progress=1.0,
        message="done",
        output_mkv="",
        output_srt="",
        work_dir="",
        log_path="",
        series_title=series_title,
        series_slug=series_slug,
        season_number=season_number,
        episode_number=episode_number,
        visibility=Visibility.private,
    )


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

            store.put(
                _job(
                    job_id="job_a1",
                    owner_id=user_a.id,
                    series_title="Alpha",
                    series_slug="alpha",
                    season_number=1,
                    episode_number=1,
                    created_at="2026-01-01T00:00:00+00:00",
                )
            )
            store.put(
                _job(
                    job_id="job_a2",
                    owner_id=user_a.id,
                    series_title="Bravo",
                    series_slug="bravo",
                    season_number=2,
                    episode_number=3,
                    created_at="2026-01-02T00:00:00+00:00",
                )
            )
            store.put(
                _job(
                    job_id="job_b1",
                    owner_id=user_b.id,
                    series_title="Charlie",
                    series_slug="charlie",
                    season_number=3,
                    episode_number=1,
                    created_at="2026-01-03T00:00:00+00:00",
                )
            )

            r = c.get("/api/library/search?q=2", headers=headers_a)
            assert r.status_code == 200, r.text
            assert len(r.json()) == 1

            r = c.get("/api/library/recent?limit=2", headers=headers_a)
            assert r.status_code == 200, r.text
            assert r.json()[0]["job_id"] == "job_a2"

            store.record_view(
                user_id=user_a.id,
                series_slug="alpha",
                season_number=1,
                episode_number=1,
                job_id="job_a1",
                opened_at=10.0,
            )
            store.record_view(
                user_id=user_a.id,
                series_slug="bravo",
                season_number=2,
                episode_number=3,
                job_id="job_a2",
                opened_at=20.0,
            )

            r = c.get("/api/library/continue?limit=10", headers=headers_a)
            assert r.status_code == 200, r.text
            assert r.json()[0]["series_slug"] == "bravo"

            r = c.get("/api/library/continue?limit=10", headers=headers_b)
            assert r.status_code == 200, r.text
            assert len(r.json()) == 1

    print("verify_library_browse: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
