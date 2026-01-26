from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.api.models import Role, User, now_ts
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState, Visibility
from dubbing_pipeline.server import app
from dubbing_pipeline.utils.crypto import PasswordHasher, random_id
from tests._helpers.auth import login_user
from tests._helpers.runtime_paths import configure_runtime_paths


def _setup_env(tmp_path: Path) -> tuple[Path, Path]:
    in_dir, out_dir, _logs_dir = configure_runtime_paths(tmp_path)
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()
    return in_dir, out_dir


def _create_users(c: TestClient) -> tuple[User, User]:
    auth = c.app.state.auth_store
    ph = PasswordHasher()
    owner = User(
        id=random_id("u_", 16),
        username="owner_vis",
        password_hash=ph.hash("pass_owner"),
        role=Role.operator,
        totp_secret=None,
        totp_enabled=False,
        created_at=now_ts(),
    )
    other = User(
        id=random_id("u_", 16),
        username="other_vis",
        password_hash=ph.hash("pass_other"),
        role=Role.viewer,
        totp_secret=None,
        totp_enabled=False,
        created_at=now_ts(),
    )
    auth.upsert_user(owner)
    auth.upsert_user(other)
    return owner, other


def _create_job(store, *, out_dir: Path, in_dir: Path, owner: User) -> tuple[str, Path]:
    job_id = "job_private_visibility"
    job_dir = out_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    output_mp4 = job_dir / "dub.mp4"
    output_mp4.write_bytes(b"\x00" * 16)
    job = Job(
        id=job_id,
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
        output_mkv=str(output_mp4),
        output_srt="",
        work_dir=str(job_dir),
        log_path=str(job_dir / "job.log"),
        series_title="Series Private",
        series_slug="series-private",
        season_number=1,
        episode_number=1,
    )
    assert job.visibility == Visibility.private
    store.put(job)
    return job_id, output_mp4


def test_private_artifacts_not_fetchable(tmp_path: Path) -> None:
    in_dir, out_dir = _setup_env(tmp_path)
    with TestClient(app) as c:
        store = c.app.state.job_store
        owner, other = _create_users(c)
        headers_owner = login_user(
            c, username=owner.username, password="pass_owner", clear_cookies=True
        )
        headers_other = login_user(
            c, username=other.username, password="pass_other", clear_cookies=True
        )
        job_id, output_mp4 = _create_job(store, out_dir=out_dir, in_dir=in_dir, owner=owner)
        rel = output_mp4.relative_to(out_dir).as_posix()

        # Private: non-owner cannot access job, files, library, or artifacts by path.
        assert c.get(f"/api/jobs/{job_id}", headers=headers_other).status_code == 403
        assert c.get(f"/api/jobs/{job_id}/files", headers=headers_other).status_code == 403
        assert c.get(f"/files/{rel}", headers=headers_other).status_code == 403
        assert (
            c.get("/api/library/series-private/seasons", headers=headers_other).status_code == 403
        )

        # Share explicitly as owner, then shared access is allowed for shared-safe endpoints only.
        shared = c.post(
            f"/api/jobs/{job_id}/visibility",
            headers=headers_owner,
            json={"visibility": "shared"},
        )
        assert shared.status_code == 200
        assert c.get(f"/api/jobs/{job_id}", headers=headers_other).status_code == 403
        assert c.get(f"/api/jobs/{job_id}/files", headers=headers_other).status_code == 200
        assert c.get(f"/files/{rel}", headers=headers_other).status_code in {200, 206}
        assert (
            c.get("/api/library/series-private/seasons", headers=headers_other).status_code == 200
        )


def test_visibility_guard_invoked_for_routes(tmp_path: Path, monkeypatch) -> None:
    in_dir, out_dir = _setup_env(tmp_path)
    with TestClient(app) as c:
        store = c.app.state.job_store
        owner, _other = _create_users(c)
        headers_owner = login_user(
            c, username=owner.username, password="pass_owner", clear_cookies=True
        )
        job_id, output_mp4 = _create_job(store, out_dir=out_dir, in_dir=in_dir, owner=owner)
        rel = output_mp4.relative_to(out_dir).as_posix()

        from dubbing_pipeline.security import visibility as vis

        calls = {"job": 0, "artifact": 0, "library": 0}
        orig_job = vis.require_can_view_job
        orig_artifact = vis.require_can_view_artifact
        orig_library = vis.require_can_view_library_item

        def spy_job(*args, **kwargs):
            calls["job"] += 1
            return orig_job(*args, **kwargs)

        def spy_artifact(*args, **kwargs):
            calls["artifact"] += 1
            return orig_artifact(*args, **kwargs)

        def spy_library(*args, **kwargs):
            calls["library"] += 1
            return orig_library(*args, **kwargs)

        monkeypatch.setattr(vis, "require_can_view_job", spy_job)
        monkeypatch.setattr(vis, "require_can_view_artifact", spy_artifact)
        monkeypatch.setattr(vis, "require_can_view_library_item", spy_library)

        assert c.get(f"/api/jobs/{job_id}", headers=headers_owner).status_code == 200
        assert c.get(f"/api/jobs/{job_id}/files", headers=headers_owner).status_code == 200
        assert c.get(f"/files/{rel}", headers=headers_owner).status_code in {200, 206}
        assert c.get("/api/library/series-private/seasons", headers=headers_owner).status_code == 200

        assert calls["job"] >= 2
        assert calls["artifact"] >= 1
        assert calls["library"] >= 1
