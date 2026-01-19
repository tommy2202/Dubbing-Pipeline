#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import time
import wave
from pathlib import Path


def _write_silence_wav(path: Path, *, seconds: float = 1.0, sr: int = 16000) -> None:
    n = int(seconds * sr)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b"\x00\x00" * n)


def main() -> int:
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception as ex:
        print(f"verify_voice_mapping_ui: SKIP (fastapi unavailable: {ex})")
        return 0

    repo_root = Path(__file__).resolve().parents[1]
    src_root = repo_root / "src"
    for p in (str(repo_root), str(src_root)):
        if p not in sys.path:
            sys.path.insert(0, p)

    try:
        from dubbing_pipeline.api.models import AuthStore, Role, User, now_ts
        from dubbing_pipeline.api.routes_auth import router as auth_router
        from dubbing_pipeline.jobs.models import Job, JobState, now_utc
        from dubbing_pipeline.jobs.store import JobStore
        from dubbing_pipeline.web.routes_jobs import router as jobs_router
        from dubbing_pipeline.utils.crypto import PasswordHasher
    except Exception as ex:
        print(f"verify_voice_mapping_ui: SKIP (imports unavailable: {ex})")
        return 0

    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        inp = (root / "Input").resolve()
        out = (root / "Output").resolve()
        logs = (root / "logs").resolve()
        inp.mkdir(parents=True, exist_ok=True)
        out.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)

        os.environ["APP_ROOT"] = str(root)
        os.environ["INPUT_DIR"] = str(inp)
        os.environ["DUBBING_OUTPUT_DIR"] = str(out)
        os.environ["DUBBING_LOG_DIR"] = str(logs)
        os.environ["COOKIE_SECURE"] = "0"
        os.environ["STRICT_SECRETS"] = "0"
        os.environ["VOICE_STORE"] = str(root / "voice_store")

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
                id="u1",
                username="user1",
                password_hash=ph.hash("pass1"),
                role=Role.operator,
                totp_secret=None,
                totp_enabled=False,
                created_at=now_ts(),
            )
        )

        job_id = "job_voice_map"
        base_dir = (out / job_id).resolve()
        (base_dir / "analysis" / "voice_refs").mkdir(parents=True, exist_ok=True)
        ref_path = base_dir / "analysis" / "voice_refs" / "SPEAKER_01_ref.wav"
        _write_silence_wav(ref_path, seconds=1.0)

        manifest = {
            "version": 1,
            "created_at": time.time(),
            "items": {
                "SPEAKER_01": {
                    "ref_path": str(ref_path),
                    "duration_s": 1.0,
                    "target_s": 10.0,
                    "warnings": [],
                }
            },
        }
        (base_dir / "analysis" / "voice_refs" / "manifest.json").write_text(
            __import__("json").dumps(manifest, indent=2),
            encoding="utf-8",
        )

        job_store.put(
            Job(
                id=job_id,
                owner_id="u1",
                video_path=str(inp / "a.mp4"),
                duration_s=1.0,
                mode="low",
                device="cpu",
                src_lang="auto",
                tgt_lang="en",
                created_at=now_utc(),
                updated_at=now_utc(),
                state=JobState.DONE,
                progress=1.0,
                message="Done",
                output_mkv=str(out / job_id / "dub.mkv"),
                output_srt="",
                work_dir=str(base_dir),
                log_path=str(logs / f"{job_id}.log"),
                error=None,
                series_title="Voice Series",
                series_slug="voice-series",
                season_number=1,
                episode_number=1,
            )
        )

        app.include_router(auth_router)
        app.include_router(jobs_router)

        with TestClient(app) as c:
            login = c.post(
                "/auth/login", json={"username": "user1", "password": "pass1", "session": True}
            )
            assert login.status_code == 200, login.text
            csrf = login.json().get("csrf_token")
            assert csrf

            r1 = c.get(f"/api/jobs/{job_id}/speakers")
            assert r1.status_code == 200, r1.text
            data = r1.json()
            assert data.get("available") is True, data
            assert data.get("items"), data

            r2 = c.post(
                f"/api/jobs/{job_id}/voice-map",
                json={
                    "items": [
                        {"speaker_id": "SPEAKER_01", "character_name": "Rimuru"}
                    ]
                },
                headers={"X-CSRF-Token": csrf},
            )
            assert r2.status_code == 200, r2.text
            assert r2.json().get("ok") is True

            r3 = c.post(
                "/api/series/voice-series/voices",
                json={
                    "confirm": True,
                    "job_id": job_id,
                    "speaker_id": "SPEAKER_01",
                    "character_name": "Rimuru",
                },
                headers={"X-CSRF-Token": csrf},
            )
            assert r3.status_code == 200, r3.text
            ref_path_out = r3.json().get("ref_path")
            assert ref_path_out and Path(ref_path_out).exists(), r3.json()

    print("verify_voice_mapping_ui: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
