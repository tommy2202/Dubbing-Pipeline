#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def _need_tool(name: str) -> None:
    if shutil.which(name):
        return
    raise RuntimeError(f"Missing required tool: {name}. Install it and retry.")


def _run(cmd: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, text=True, capture_output=True, timeout=timeout)  # nosec B603


def _make_tiny_mp4(path: Path) -> None:
    _need_tool("ffmpeg")
    p = _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "1.0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        timeout=60,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip() or "ffmpeg failed")


def main() -> int:
    try:
        _need_tool("ffmpeg")
    except RuntimeError as ex:
        print(f"e2e_restart_worker_midrun: SKIP ({ex})")
        return 0

    from dubbing_pipeline.jobs.queue import JobQueue
    from dubbing_pipeline.jobs.models import Job, JobState, now_utc
    from dubbing_pipeline.jobs.store import JobStore

    class _NoopJobQueue(JobQueue):
        async def _worker(self) -> None:
            try:
                while True:
                    await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                return

    async def _run() -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            inp = (root / "Input").resolve()
            out = (root / "Output").resolve()
            inp.mkdir(parents=True, exist_ok=True)
            out.mkdir(parents=True, exist_ok=True)
            os.environ["DUBBING_OUTPUT_DIR"] = str(out)
            os.environ["STRICT_SECRETS"] = "0"

            mp4 = inp / "tiny.mp4"
            _make_tiny_mp4(mp4)

            store = JobStore(root / "jobs.db")
            job_id = "job_restart_midrun"
            store.put(
                Job(
                    id=job_id,
                    owner_id="u1",
                    video_path=str(mp4),
                    duration_s=1.0,
                    mode="low",
                    device="cpu",
                    src_lang="auto",
                    tgt_lang="en",
                    created_at=now_utc(),
                    updated_at=now_utc(),
                    state=JobState.RUNNING,
                    progress=0.3,
                    message="Running before restart",
                    output_mkv="",
                    output_srt="",
                    work_dir=str(out),
                    log_path=str(out / "job.log"),
                    error=None,
                )
            )

            q = _NoopJobQueue(store, concurrency=1)
            await q.start()
            await asyncio.sleep(0.1)

            j = store.get(job_id)
            assert j is not None
            assert j.state == JobState.QUEUED, f"expected QUEUED, got {j.state}"
            assert "Recovered after restart" in str(j.message)

            await q.stop()

    try:
        asyncio.run(_run())
    except Exception as ex:
        print("e2e_restart_worker_midrun: FAIL")
        print(f"- error: {ex}")
        return 2

    print("e2e_restart_worker_midrun: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
