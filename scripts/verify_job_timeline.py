from __future__ import annotations

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

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.checkpoint import (
    record_stage_finished,
    record_stage_skipped,
    record_stage_started,
)
from dubbing_pipeline.jobs.models import Job, JobState, now_utc
from dubbing_pipeline.server import app


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
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()


def _make_job(job_id: str, owner_id: str, out_dir: Path) -> Job:
    now = now_utc()
    out_dir.mkdir(parents=True, exist_ok=True)
    return Job(
        id=job_id,
        owner_id=str(owner_id),
        video_path="",
        duration_s=0.0,
        mode="fast",
        device="cpu",
        src_lang="en",
        tgt_lang="en",
        created_at=now,
        updated_at=now,
        state=JobState.RUNNING,
        progress=0.5,
        message="Working",
        output_mkv=str(out_dir / "job.dub.mkv"),
        output_srt=str(out_dir / "job.srt"),
        work_dir=str(out_dir / "work" / job_id),
        log_path=str(out_dir / "job.log"),
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="job-timeline-") as td:
        root = Path(td)
        _set_env(root)
        with TestClient(app) as client:
            resp = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "adminpass"},
            )
            if resp.status_code != 200:
                print("login failed", resp.status_code, resp.text)
                return 1
            access = resp.json().get("access_token")
            if not access:
                print("missing access token")
                return 1
            headers = {"Authorization": f"Bearer {access}"}

            auth_store = client.app.state.auth_store
            user = auth_store.get_user_by_username("admin")
            if user is None:
                print("admin user missing")
                return 1

            job_id = f"job_{uuid.uuid4().hex[:8]}"
            out_dir = Path(os.environ["DUBBING_OUTPUT_DIR"]) / job_id
            job = _make_job(job_id, str(user.id), out_dir)
            client.app.state.job_store.put(job)
            client.app.state.job_store.append_log(job_id, "[test] pipeline initialized")

            ckpt_path = out_dir / ".checkpoint.json"
            record_stage_started(job_id, "extracting", ckpt_path=ckpt_path)
            record_stage_finished(job_id, "extracting", ckpt_path=ckpt_path)
            record_stage_started(job_id, "asr", ckpt_path=ckpt_path)
            record_stage_finished(job_id, "asr", ckpt_path=ckpt_path)
            record_stage_started(job_id, "translation", ckpt_path=ckpt_path)
            record_stage_skipped(job_id, "translation", ckpt_path=ckpt_path, reason="same_language")
            record_stage_started(job_id, "tts", ckpt_path=ckpt_path)
            record_stage_finished(job_id, "tts", ckpt_path=ckpt_path)
            record_stage_started(job_id, "mixing", ckpt_path=ckpt_path)
            record_stage_finished(job_id, "mixing", ckpt_path=ckpt_path)
            record_stage_started(job_id, "export", ckpt_path=ckpt_path)
            record_stage_finished(job_id, "export", ckpt_path=ckpt_path)

            timeline = client.get(f"/api/jobs/{job_id}/timeline", headers=headers)
            if timeline.status_code != 200:
                print("timeline failed", timeline.status_code, timeline.text)
                return 1
            data = timeline.json()
            stages = data.get("stages")
            if not isinstance(stages, list) or len(stages) < 7:
                print("missing stages", stages)
                return 1
            if not data.get("current_stage"):
                print("missing current stage")
                return 1
            if not data.get("last_log_line"):
                print("missing last log line")
                return 1
            skipped = [s for s in stages if s.get("stage") == "translation"]
            if not skipped or skipped[0].get("status") != "skipped":
                print("translation skip missing", skipped)
                return 1
            for stage in stages:
                if "duration_s" not in stage:
                    print("duration missing", stage)
                    return 1
            print("ok")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
