from __future__ import annotations

import json
import os
from pathlib import Path

from dubbing_pipeline.api.deps import Identity
from dubbing_pipeline.api.models import Role, User, now_ts
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState, Visibility, now_utc
from dubbing_pipeline.jobs.store import JobStore
from dubbing_pipeline.library import queries
from dubbing_pipeline.library.manifest import write_manifest
from dubbing_pipeline.library.normalize import normalize_series_title, series_to_slug
from dubbing_pipeline.library.paths import ensure_library_dir
from dubbing_pipeline.library.registry import read_registry, repair_manifest_registry
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


def _write_manifest(job: Job) -> Path:
    lib_dir = ensure_library_dir(job)
    assert lib_dir is not None
    return write_manifest(
        job=job,
        outputs={
            "library_dir": str(lib_dir),
            "master": "",
            "mobile": "",
            "hls_index": "",
            "logs_dir": str(lib_dir / "logs"),
            "qa_dir": str(lib_dir / "qa"),
        },
    )


def test_library_migration_no_orphans(tmp_path: Path) -> None:
    out_dir = _setup_env(tmp_path)
    store = JobStore(out_dir / "_state" / "jobs.db")
    owner = _make_user("u_owner")
    ident = Identity(kind="user", user=owner, scopes=["read:job"])

    series = "Series S"
    job1 = _make_job(
        job_id="job_ep1",
        owner_id=owner.id,
        series_title=series,
        season=1,
        episode=1,
        visibility=Visibility.private,
    )
    job2 = _make_job(
        job_id="job_ep2",
        owner_id=owner.id,
        series_title=series,
        season=1,
        episode=2,
        visibility=Visibility.shared,
    )
    job3 = _make_job(
        job_id="job_ep3",
        owner_id=owner.id,
        series_title=series,
        season=1,
        episode=3,
        visibility=Visibility.private,
    )
    store.put(job1)
    store.put(job2)
    store.put(job3)

    manifest1 = _write_manifest(job1)
    manifest2 = _write_manifest(job2)
    manifest3 = _write_manifest(job3)

    # Simulate missing visibility in one manifest.
    data = json.loads(manifest3.read_text(encoding="utf-8"))
    data.pop("visibility", None)
    manifest3.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Rename series and reorder episodes in the job store (migration inputs).
    new_title = "Series Renamed"
    new_slug = series_to_slug(normalize_series_title(new_title))
    store.update(job1.id, series_title=new_title, series_slug=new_slug, episode_number=1)
    store.update(job2.id, series_title=new_title, series_slug=new_slug, episode_number=3)
    store.update(job3.id, series_title=new_title, series_slug=new_slug, episode_number=2)

    # Browse order uses the indexed job_library table.
    items, _meta = queries.list_episodes(
        store=store, ident=ident, series_slug=new_slug, season_number=1
    )
    got_order = [int(it["episode_number"]) for it in items]
    assert got_order == [1, 2, 3]

    # Repair registry preserves visibility and defaults missing to private.
    repair_manifest_registry(store=store, output_root=out_dir)
    entries = read_registry(output_root=out_dir)
    assert entries[job1.id]["visibility"] == "private"
    assert entries[job2.id]["visibility"] == "shared"
    assert entries[job3.id]["visibility"] == "private"

    # Delete an episode artifact and ensure no orphaned registry entry remains.
    manifest2.unlink()
    repair_manifest_registry(store=store, output_root=out_dir)
    entries2 = read_registry(output_root=out_dir)
    assert job2.id not in entries2
    assert job1.id in entries2
    assert job3.id in entries2
