#!/usr/bin/env python3
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _login(c: TestClient, username: str, password: str) -> str:
    r = c.post("/auth/login", json={"username": username, "password": password, "session": True})
    assert r.status_code == 200, r.text
    token = r.json().get("csrf_token") or ""
    assert token
    return str(token)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        out = (root / "Output").resolve()
        logs = (root / "logs").resolve()
        out.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)

        os.environ["APP_ROOT"] = str(root)
        os.environ["DUBBING_OUTPUT_DIR"] = str(out)
        os.environ["DUBBING_LOG_DIR"] = str(logs)
        os.environ["COOKIE_SECURE"] = "0"
        os.environ["STRICT_SECRETS"] = "0"

        from dubbing_pipeline.api.models import AuthStore, Role, User, now_ts
        from dubbing_pipeline.api.routes_auth import router as auth_router
        from dubbing_pipeline.jobs.checkpoint import (
            record_stage_skipped,
            record_stage_started,
            write_ckpt,
        )
        from dubbing_pipeline.jobs.models import Job, JobState, now_utc
        from dubbing_pipeline.jobs.store import JobStore
        from dubbing_pipeline.utils.crypto import PasswordHasher
        from dubbing_pipeline.web.routes_jobs import router as jobs_router

        app = FastAPI()
        state_root = (out / "_state").resolve()
        state_root.mkdir(parents=True, exist_ok=True)
        job_store = JobStore(state_root / "jobs.db")
        app.state.job_store = job_store

        auth_store = AuthStore(state_root / "auth.db")
        app.state.auth_store = auth_store

        ph = PasswordHasher()
        auth_store.upsert_user(
            User(
                id="u_admin",
                username="admin",
                password_hash=ph.hash("adminpass"),
                role=Role.admin,
                totp_secret=None,
                totp_enabled=False,
                created_at=now_ts(),
            )
        )

        app.include_router(auth_router)
        app.include_router(jobs_router)

        # Create a job + checkpoint timeline
        job_id = "job_timeline"
        base_dir = (out / "job_timeline").resolve()
        base_dir.mkdir(parents=True, exist_ok=True)
        ckpt_path = base_dir / ".checkpoint.json"

        # dummy artifacts for checkpoints
        audio_wav = base_dir / "audio.wav"
        srt_out = base_dir / "job_timeline.srt"
        tts_wav = base_dir / "job_timeline.tts.wav"
        mix_mkv = base_dir / "job_timeline.dub.mkv"
        for p in (audio_wav, srt_out, tts_wav, mix_mkv):
            p.write_bytes(b"")

        log_path = base_dir / "job.log"
        log_path.write_text("[2026-01-01T00:00:00Z] queued\n[2026-01-01T00:00:01Z] extract\n")

        job_store.put(
            Job(
                id=job_id,
                owner_id="u_admin",
                video_path=str(base_dir / "job_timeline.mp4"),
                duration_s=1.0,
                mode="low",
                device="cpu",
                src_lang="en",
                tgt_lang="en",
                created_at=now_utc(),
                updated_at=now_utc(),
                state=JobState.RUNNING,
                progress=0.3,
                message="Running",
                output_mkv=str(mix_mkv),
                output_srt=str(srt_out),
                work_dir=str(base_dir),
                log_path=str(log_path),
                error=None,
            )
        )

        record_stage_started(job_id, "audio", ckpt_path=ckpt_path)
        write_ckpt(job_id, "audio", {"audio_wav": audio_wav}, {"work_dir": str(base_dir)}, ckpt_path=ckpt_path)
        record_stage_started(job_id, "transcribe", ckpt_path=ckpt_path)
        write_ckpt(job_id, "transcribe", {"srt": srt_out}, {"work_dir": str(base_dir)}, ckpt_path=ckpt_path)
        record_stage_skipped(job_id, "translate", "same_language", ckpt_path=ckpt_path)
        record_stage_started(job_id, "tts", ckpt_path=ckpt_path)
        write_ckpt(job_id, "tts", {"tts_wav": tts_wav}, {"work_dir": str(base_dir)}, ckpt_path=ckpt_path)
        record_stage_started(job_id, "mix", ckpt_path=ckpt_path)
        write_ckpt(job_id, "mix", {"mkv": mix_mkv}, {"work_dir": str(base_dir)}, ckpt_path=ckpt_path)
        record_stage_skipped(job_id, "mux", "combined_with_mix", ckpt_path=ckpt_path)

        with TestClient(app) as c:
            _login(c, "admin", "adminpass")
            r = c.get(f"/api/jobs/{job_id}/timeline")
            assert r.status_code == 200, r.text
            data = r.json()
            stages = data.get("stages") if isinstance(data, dict) else None
            assert isinstance(stages, list) and len(stages) == 7, stages
            keys = [s.get("key") for s in stages]
            assert keys == ["queued", "extract", "asr", "translate", "tts", "mix", "export"], keys
            translate = [s for s in stages if s.get("key") == "translate"][0]
            assert translate.get("status") == "skipped", translate
            assert translate.get("reason") in {"same_language", "imported_transcript"}, translate
            export = [s for s in stages if s.get("key") == "export"][0]
            assert export.get("status") == "skipped", export
            assert "combined_with_mix" in str(export.get("reason") or "")
            assert data.get("last_log_line"), data

    print("verify_job_timeline: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
