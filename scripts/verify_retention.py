from __future__ import annotations

import os
import tempfile
from pathlib import Path

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState, Visibility, now_utc
from dubbing_pipeline.jobs.store import JobStore
from dubbing_pipeline.ops.retention import run_once


def _make_job(
    *,
    job_id: str,
    owner_id: str,
    in_dir: Path,
    out_dir: Path,
    updated_at: str,
    runtime: dict | None = None,
) -> Job:
    job_dir = out_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    output_mkv = job_dir / f"{job_id}.dub.mp4"
    output_mkv.write_bytes(b"\x00" * 8)
    log_path = job_dir / "job.log"
    log_path.write_text("log\n", encoding="utf-8")
    return Job(
        id=job_id,
        owner_id=owner_id,
        video_path=str(in_dir / "source.mp4"),
        duration_s=10.0,
        mode="low",
        device="cpu",
        src_lang="ja",
        tgt_lang="en",
        created_at=updated_at,
        updated_at=updated_at,
        state=JobState.DONE,
        progress=1.0,
        message="done",
        output_mkv=str(output_mkv),
        output_srt="",
        work_dir=str(job_dir),
        log_path=str(log_path),
        visibility=Visibility.private,
        runtime=runtime or {},
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        in_dir = root / "Input"
        out_dir = root / "Output"
        logs_dir = root / "logs"
        state_dir = root / "_state"
        in_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)

        os.environ["APP_ROOT"] = str(root)
        os.environ["INPUT_DIR"] = str(in_dir)
        os.environ["DUBBING_OUTPUT_DIR"] = str(out_dir)
        os.environ["DUBBING_LOG_DIR"] = str(logs_dir)
        os.environ["DUBBING_STATE_DIR"] = str(state_dir)
        os.environ["RETENTION_ENABLED"] = "1"
        os.environ["RETENTION_UPLOAD_TTL_HOURS"] = "1"
        os.environ["RETENTION_JOB_ARTIFACT_DAYS"] = "1"
        os.environ["RETENTION_LOG_DAYS"] = "1"
        os.environ["RETENTION_INTERVAL_SEC"] = "0"
        get_settings.cache_clear()

        store = JobStore(state_dir / "jobs.db")
        old_ts = "2024-01-01T00:00:00+00:00"
        job_old = _make_job(
            job_id="job_retention_old",
            owner_id="u_1",
            in_dir=in_dir,
            out_dir=out_dir,
            updated_at=old_ts,
        )
        job_pinned = _make_job(
            job_id="job_retention_pinned",
            owner_id="u_1",
            in_dir=in_dir,
            out_dir=out_dir,
            updated_at=old_ts,
            runtime={"pinned": True},
        )
        store.put(job_old)
        store.put(job_pinned)

        uploads_dir = (in_dir / "uploads").resolve()
        uploads_dir.mkdir(parents=True, exist_ok=True)
        upload_id = "up_retention_1"
        part_path = uploads_dir / f"{upload_id}.part"
        part_path.write_bytes(b"\x01" * 4)
        store.put_upload(
            upload_id,
            {
                "id": upload_id,
                "owner_id": "u_1",
                "filename": "x.mp4",
                "total_bytes": 4,
                "chunk_bytes": 4,
                "received": {},
                "received_bytes": 0,
                "completed": False,
                "part_path": str(part_path),
                "final_path": str(uploads_dir / f"{upload_id}_x.mp4"),
                "created_at": old_ts,
                "updated_at": old_ts,
            },
        )

        res = run_once(store=store, output_root=out_dir, app_root=root)
        if res.uploads_removed < 1 or res.jobs_removed < 1:
            print("FAIL: retention did not remove expected items")
            return 1
        if store.get("job_retention_old") is not None:
            print("FAIL: old job still present")
            return 1
        if store.get("job_retention_pinned") is None:
            print("FAIL: pinned job removed")
            return 1
        if store.get_upload(upload_id) is not None or part_path.exists():
            print("FAIL: upload not removed")
            return 1

        job_new = _make_job(
            job_id="job_new",
            owner_id="u_1",
            in_dir=in_dir,
            out_dir=out_dir,
            updated_at=now_utc(),
        )
        store.put(job_new)
        if store.get("job_new") is None:
            print("FAIL: new job missing after retention")
            return 1

    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
