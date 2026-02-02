from __future__ import annotations

import os
from pathlib import Path

from dubbing_pipeline.api.deps import Identity
from dubbing_pipeline.api.models import Role, User, now_ts
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState, Visibility, now_utc
from dubbing_pipeline.jobs.store import JobStore
from dubbing_pipeline.library import queries
from dubbing_pipeline.library.normalize import normalize_series_title, series_to_slug
from tests._helpers.runtime_paths import configure_runtime_paths


def _setup_env(tmp_path: Path) -> Path:
    _in_dir, out_dir, _logs_dir = configure_runtime_paths(tmp_path)
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()
    return out_dir


def _make_user(user_id: str, role: Role = Role.operator) -> User:
    return User(
        id=user_id,
        username=f"user_{user_id}",
        password_hash="x",
        role=role,
        totp_secret=None,
        totp_enabled=False,
        created_at=now_ts(),
    )


def _make_job(
    *,
    job_id: str,
    owner_id: str,
    series_title: str,
    season: int,
    episode: int,
    visibility: Visibility,
) -> Job:
    created = now_utc()
    title = normalize_series_title(series_title)
    slug = series_to_slug(title)
    return Job(
        id=job_id,
        owner_id=owner_id,
        video_path="/dev/null",
        duration_s=0.0,
        mode="low",
        device="cpu",
        src_lang="ja",
        tgt_lang="en",
        created_at=created,
        updated_at=created,
        state=JobState.DONE,
        progress=1.0,
        message="done",
        output_mkv="",
        output_srt="",
        work_dir="",
        log_path="",
        series_title=title,
        series_slug=slug,
        season_number=int(season),
        episode_number=int(episode),
        visibility=visibility,
    )


def test_library_visibility_shared_global(tmp_path: Path) -> None:
    out_dir = _setup_env(tmp_path)
    store = JobStore(out_dir / "_state" / "jobs.db")
    owner = _make_user("u_owner")
    other = _make_user("u_other", role=Role.viewer)
    owner_ident = Identity(kind="user", user=owner, scopes=["read:job"])
    other_ident = Identity(kind="user", user=other, scopes=["read:job"])

    series = "Shared Policy"
    job_private = _make_job(
        job_id="job_private",
        owner_id=owner.id,
        series_title=series,
        season=1,
        episode=1,
        visibility=Visibility.private,
    )
    job_shared = _make_job(
        job_id="job_shared",
        owner_id=owner.id,
        series_title=series,
        season=1,
        episode=2,
        visibility=Visibility.shared,
    )
    store.put(job_private)
    store.put(job_shared)

    slug = series_to_slug(normalize_series_title(series))
    owner_items, _ = queries.list_episodes(
        store=store, ident=owner_ident, series_slug=slug, season_number=1
    )
    owner_eps = [int(it["episode_number"]) for it in owner_items]
    assert owner_eps == [1, 2]

    other_items, _ = queries.list_episodes(
        store=store, ident=other_ident, series_slug=slug, season_number=1
    )
    other_eps = [int(it["episode_number"]) for it in other_items]
    # Shared is visible to any authenticated user, private remains hidden.
    assert other_eps == [2]
