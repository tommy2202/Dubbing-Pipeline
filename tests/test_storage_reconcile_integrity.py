from __future__ import annotations

import os
from pathlib import Path

import pytest

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState
from dubbing_pipeline.jobs.store import JobStore
from dubbing_pipeline.ops.storage import reconcile_storage_accounting


def _setup_env(tmp_path: Path) -> None:
    os.environ["APP_ROOT"] = str(tmp_path)
    os.environ["DUBBING_OUTPUT_DIR"] = str(tmp_path / "Output")
    get_settings.cache_clear()


def _make_job(*, job_id: str, owner_id: str, output_root: Path) -> Job:
    now = "2026-01-01T00:00:00+00:00"
    out_dir = output_root / "jobA"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "out.mkv"
    out_file.write_bytes(b"job-output-bytes")
    return Job(
        id=job_id,
        owner_id=owner_id,
        video_path="/tmp/input.mp4",
        duration_s=1.0,
        mode="low",
        device="cpu",
        src_lang="ja",
        tgt_lang="en",
        created_at=now,
        updated_at=now,
        state=JobState.DONE,
        progress=1.0,
        message="Done",
        output_mkv=str(out_file),
        output_srt="",
        work_dir=str(out_dir),
        log_path=str(out_dir / "job.log"),
    )


def test_storage_reconcile_updates_bytes(tmp_path: Path) -> None:
    _setup_env(tmp_path)
    output_root = Path(os.environ["DUBBING_OUTPUT_DIR"]).resolve()
    store = JobStore(tmp_path / "jobs.db")

    job = _make_job(job_id="job1", owner_id="userA", output_root=output_root)
    store.put(job)

    uploads_root = (tmp_path / "Input" / "uploads").resolve()
    uploads_root.mkdir(parents=True, exist_ok=True)
    upload_path = uploads_root / "up1_file.mp4"
    upload_path.write_bytes(b"upload-bytes")
    store.put_upload(
        "up1",
        {
            "id": "up1",
            "owner_id": "userA",
            "final_path": str(upload_path),
            "completed": True,
        },
    )

    store.set_job_storage_bytes("job1", user_id="userA", bytes_count=99999)
    store.set_upload_storage_bytes("up1", user_id="userA", bytes_count=88888)

    reconcile_storage_accounting(
        store=store,
        output_root=output_root,
        app_root=tmp_path,
    )
    expected = (output_root / "jobA" / "out.mkv").stat().st_size + upload_path.stat().st_size
    assert store.get_user_storage_bytes("userA") == expected

    upload_path.unlink()
    reconcile_storage_accounting(
        store=store,
        output_root=output_root,
        app_root=tmp_path,
    )
    expected = (output_root / "jobA" / "out.mkv").stat().st_size
    assert store.get_user_storage_bytes("userA") == expected


def test_storage_reconcile_symlink_trap(tmp_path: Path) -> None:
    _setup_env(tmp_path)
    store = JobStore(tmp_path / "jobs.db")
    uploads_root = (tmp_path / "Input" / "uploads").resolve()
    uploads_root.mkdir(parents=True, exist_ok=True)

    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret-bytes")
    link_path = uploads_root / "link.bin"

    try:
        os.symlink(outside, link_path)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not supported on this platform")

    store.put_upload(
        "up_symlink",
        {
            "id": "up_symlink",
            "owner_id": "userA",
            "final_path": str(link_path),
            "completed": True,
        },
    )

    reconcile_storage_accounting(
        store=store,
        output_root=Path(os.environ["DUBBING_OUTPUT_DIR"]).resolve(),
        app_root=tmp_path,
    )
    assert store.get_user_storage_bytes("userA") == 0
