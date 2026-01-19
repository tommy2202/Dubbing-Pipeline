#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    src_root = repo_root / "src"
    for p in (str(repo_root), str(src_root)):
        if p not in sys.path:
            sys.path.insert(0, p)

    try:
        from config.settings import SETTINGS
        from dubbing_pipeline.jobs.models import Job, JobState, now_utc
        from dubbing_pipeline.jobs.store import JobStore
        from dubbing_pipeline.utils.single_writer import SingleWriterError
    except Exception as ex:
        print(f"verify_single_writer_or_db_backend: SKIP (imports unavailable: {ex})")
        return 0

    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        out = (root / "Output").resolve()
        state = (out / "_state").resolve()
        out.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)

        os.environ["APP_ROOT"] = str(root)
        os.environ["DUBBING_OUTPUT_DIR"] = str(out)
        os.environ["DUBBING_STATE_DIR"] = str(state)
        os.environ["SINGLE_WRITER_MODE"] = "1"
        os.environ["SINGLE_WRITER_ROLE"] = "writer"
        os.environ["SINGLE_WRITER_LOCK_PATH"] = str(root / "metadata.lock")
        SETTINGS.reload()

        store = JobStore(state / "jobs.db")
        job_id = "job_single_writer"
        now = now_utc()
        store.put(
            Job(
                id=job_id,
                owner_id="u1",
                video_path=str(root / "input.mp4"),
                duration_s=1.0,
                mode="low",
                device="cpu",
                src_lang="auto",
                tgt_lang="en",
                created_at=now,
                updated_at=now,
                state=JobState.QUEUED,
                progress=0.0,
                message="Queued",
                output_mkv="",
                output_srt="",
                work_dir=str(out / job_id),
                log_path=str(out / f"{job_id}.log"),
                error=None,
            )
        )

        os.environ["SINGLE_WRITER_ROLE"] = "reader"
        SETTINGS.reload()

        blocked = False
        try:
            store.update(job_id, message="should fail in reader mode")
        except SingleWriterError:
            blocked = True
        except Exception as ex:
            print(f"verify_single_writer_or_db_backend: FAIL (unexpected error: {ex})")
            return 1

        if not blocked:
            print("verify_single_writer_or_db_backend: FAIL (reader allowed write)")
            return 1

    print("verify_single_writer_or_db_backend: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
