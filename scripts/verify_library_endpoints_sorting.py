from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from anime_v2.api.models import AuthStore, Role, User, now_ts
from anime_v2.api.routes_library import router as library_router
from anime_v2.api.security import create_access_token
from anime_v2.jobs.models import Job, JobState, Visibility
from anime_v2.jobs.store import JobStore
from anime_v2.utils.crypto import PasswordHasher
from anime_v2.utils.ratelimit import RateLimiter


def _make_user(*, user_id: str, role: Role) -> User:
    ph = PasswordHasher()
    return User(
        id=user_id,
        username=user_id,
        password_hash=ph.hash("pw"),
        role=role,
        totp_secret=None,
        totp_enabled=False,
        created_at=now_ts(),
    )


def _token(user: User) -> str:
    return create_access_token(
        sub=user.id,
        role=str(user.role.value),
        scopes=["read:job"],
        minutes=60,
    )


def _job(
    *,
    job_id: str,
    owner_id: str,
    series_title: str,
    series_slug: str,
    season: int,
    episode: int,
    updated_at: str,
    visibility: Visibility,
) -> Job:
    return Job(
        id=job_id,
        owner_id=owner_id,
        video_path="/dev/null",
        duration_s=1.0,
        mode="low",
        device="cpu",
        src_lang="ja",
        tgt_lang="en",
        created_at=updated_at,
        updated_at=updated_at,
        state=JobState.DONE,
        progress=1.0,
        message="Done",
        output_mkv="",
        output_srt="",
        work_dir="",
        log_path="",
        error=None,
        series_title=series_title,
        series_slug=series_slug,
        season_number=int(season),
        episode_number=int(episode),
        visibility=visibility,
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        out = root / "Output"
        state = out / "_state"
        state.mkdir(parents=True, exist_ok=True)

        # Ensure consistent secrets/config.
        os.environ["ANIME_V2_OUTPUT_DIR"] = str(out)
        os.environ["ANIME_V2_APP_ROOT"] = str(root)
        os.environ["ANIME_V2_JWT_SECRET"] = "test-secret-please-change"
        os.environ["ANIME_V2_CSRF_SECRET"] = "test-secret-please-change"
        os.environ["ANIME_V2_SESSION_SECRET"] = "test-secret-please-change"

        auth = AuthStore(state / "auth.db")
        u_admin = _make_user(user_id="u_admin", role=Role.admin)
        u_alice = _make_user(user_id="u_alice", role=Role.operator)
        u_bob = _make_user(user_id="u_bob", role=Role.operator)
        auth.upsert_user(u_admin)
        auth.upsert_user(u_alice)
        auth.upsert_user(u_bob)

        store = JobStore(state / "jobs.db")

        # Create out-of-order jobs with multiple versions for episode 2.
        # Alice private series.
        store.put(
            _job(
                job_id="j_ep10",
                owner_id="u_alice",
                series_title="My Show",
                series_slug="my-show",
                season=1,
                episode=10,
                updated_at="2026-01-01T00:00:10+00:00",
                visibility=Visibility.private,
            )
        )
        store.put(
            _job(
                job_id="j_ep1",
                owner_id="u_alice",
                series_title="My Show",
                series_slug="my-show",
                season=1,
                episode=1,
                updated_at="2026-01-01T00:00:01+00:00",
                visibility=Visibility.private,
            )
        )
        # Episode 2 version A (older)
        store.put(
            _job(
                job_id="j_ep2_v1",
                owner_id="u_alice",
                series_title="My Show",
                series_slug="my-show",
                season=1,
                episode=2,
                updated_at="2026-01-01T00:00:02+00:00",
                visibility=Visibility.private,
            )
        )
        # Episode 2 version B (newer) - should be returned by default
        store.put(
            _job(
                job_id="j_ep2_v2",
                owner_id="u_alice",
                series_title="My Show",
                series_slug="my-show",
                season=1,
                episode=2,
                updated_at="2026-01-01T00:00:20+00:00",
                visibility=Visibility.private,
            )
        )

        # Bob has a public series; Alice should be able to see it.
        store.put(
            _job(
                job_id="j_pub",
                owner_id="u_bob",
                series_title="A Public Show",
                series_slug="a-public-show",
                season=1,
                episode=1,
                updated_at="2026-01-01T00:00:05+00:00",
                visibility=Visibility.public,
            )
        )
        # Bob has a private series; Alice must NOT see it.
        store.put(
            _job(
                job_id="j_priv",
                owner_id="u_bob",
                series_title="Bob Secret",
                series_slug="bob-secret",
                season=1,
                episode=1,
                updated_at="2026-01-01T00:00:06+00:00",
                visibility=Visibility.private,
            )
        )

        app = FastAPI()
        app.state.auth_store = auth
        app.state.job_store = store
        app.state.rate_limiter = RateLimiter()
        app.include_router(library_router)

        c = TestClient(app)
        h_alice = {"Authorization": f"Bearer {_token(u_alice)}"}
        h_admin = {"Authorization": f"Bearer {_token(u_admin)}"}

        # Series list sorted by title (A.. then My..), and auth filtered (no bob-secret for alice).
        r = c.get("/api/library/series", headers=h_alice)
        assert r.status_code == 200, r.text
        items = r.json()
        slugs = [x["series_slug"] for x in items]
        assert slugs == ["a-public-show", "my-show"], slugs

        # Admin sees bob-secret too.
        r2 = c.get("/api/library/series", headers=h_admin)
        assert r2.status_code == 200, r2.text
        slugs2 = [x["series_slug"] for x in r2.json()]
        assert "bob-secret" in slugs2, slugs2

        # Seasons sorted numerically ascending.
        r3 = c.get("/api/library/my-show/seasons", headers=h_alice)
        assert r3.status_code == 200, r3.text
        seasons = r3.json()
        assert [x["season_number"] for x in seasons] == [1]

        # Episodes sorted numerically ascending; episode 2 picks newest job_id.
        r4 = c.get("/api/library/my-show/1/episodes", headers=h_alice)
        assert r4.status_code == 200, r4.text
        eps = r4.json()
        assert [x["episode_number"] for x in eps] == [1, 2, 10], eps
        ep2 = [x for x in eps if x["episode_number"] == 2][0]
        assert ep2["job_id"] == "j_ep2_v2", ep2
        assert int(ep2["versions_count"]) == 2, ep2
        assert "include_versions=1" in str(ep2.get("versions_url") or "")

        # include_versions returns both versions (newest first for that episode).
        r5 = c.get(
            "/api/library/my-show/1/episodes?episode_number=2&include_versions=1",
            headers=h_alice,
        )
        assert r5.status_code == 200, r5.text
        vers = r5.json()
        assert [x["job_id"] for x in vers] == ["j_ep2_v2", "j_ep2_v1"], vers

    print("verify_library_endpoints_sorting: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

