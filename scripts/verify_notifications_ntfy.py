from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dubbing_pipeline.api.routes_settings import UserSettingsStore
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState, now_utc
from dubbing_pipeline.jobs.queue import JobQueue
from dubbing_pipeline.jobs.store import JobStore


def _set_env(root: Path) -> None:
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
    os.environ["DUBBING_SETTINGS_PATH"] = str(root / "settings.json")
    os.environ["NTFY_ENABLED"] = "1"
    os.environ["PUBLIC_BASE_URL"] = "http://example.local"
    if os.environ.get("NTFY_URL"):
        os.environ["NTFY_BASE_URL"] = os.environ.get("NTFY_URL", "")
    else:
        os.environ.setdefault("NTFY_BASE_URL", "http://ntfy.local")
    get_settings.cache_clear()


def _build_payload(*, job_id: str, filename: str, state: str, link: str) -> dict:
    title = filename or "Dubbing job finished"
    msg = f"Status: {state}\nJob: {job_id}\nLink: {link}"
    return {
        "event": f"job.{state.lower()}",
        "title": title,
        "message": msg,
        "url": link,
        "tags": ["dubbing-pipeline", state.lower()],
        "priority": 4 if state.upper() == "FAILED" else 3,
    }


def _build_attention_payload(*, job_id: str, filename: str, link: str, reasons: list[str]) -> dict:
    title = filename or "Dubbing job needs attention"
    msg = f"Warnings: {', '.join(reasons)}\nJob: {job_id}\nLink: {link}"
    return {
        "event": "job.needs_attention",
        "title": title,
        "message": msg,
        "url": link,
        "tags": ["dubbing-pipeline", "attention"],
        "priority": 4,
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="notify-verify-") as td:
        root = Path(td)
        _set_env(root)
        dry_run = not bool(os.environ.get("NTFY_URL"))

        owner_id = f"u_{uuid.uuid4().hex[:8]}"
        topic = "user-notify"
        UserSettingsStore().update_user(
            owner_id,
            {"notifications": {"notify_enabled": True, "notify_topic": topic}},
        )

        job_id = f"job_{uuid.uuid4().hex[:8]}"
        now = now_utc()
        filename = "sample.mp4"
        job = Job(
            id=job_id,
            owner_id=owner_id,
            video_path=str(root / "Input" / filename),
            duration_s=0.0,
            mode="fast",
            device="cpu",
            src_lang="en",
            tgt_lang="en",
            created_at=now,
            updated_at=now,
            state=JobState.DONE,
            progress=1.0,
            message="Done",
            output_mkv=str(root / "Output" / "out.mkv"),
            output_srt="",
            work_dir=str(root / "Output"),
            log_path=str(root / "Output" / "job.log"),
            runtime={
                "metadata": {"degraded": True, "degraded_reasons": ["budget_transcribe_exceeded"]}
            },
        )
        store = JobStore()
        store.put(job)
        q = JobQueue(store=store, app_root=root)

        link = "http://example.local/ui/jobs/" + job_id
        payloads = [
            _build_payload(job_id=job_id, filename=filename, state="DONE", link=link),
            _build_attention_payload(
                job_id=job_id,
                filename=filename,
                link=link,
                reasons=["budget_transcribe_exceeded"],
            ),
            _build_payload(job_id=job_id, filename=filename, state="FAILED", link=link),
        ]

        if dry_run:
            print("dry_run: true")
            for p in payloads:
                p["topic"] = topic
                print(json.dumps(p, indent=2))
        else:
            print("dry_run: false (sending)")

        asyncio.run(q._notify_job_finished(job_id, state="DONE", dry_run=dry_run))
        asyncio.run(q._notify_job_finished(job_id, state="FAILED", dry_run=dry_run))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
