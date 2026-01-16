#!/usr/bin/env python3
from __future__ import annotations

import os
import tempfile
import wave
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _write_tone_wav(path: Path, *, seconds: float = 0.3, sr: int = 16000) -> None:
    import math

    n = int(seconds * sr)
    amp = 0.2
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()
        for i in range(n):
            s = amp * math.sin(2 * math.pi * 440.0 * (i / sr))
            v = int(max(-1.0, min(1.0, s)) * 32767)
            frames += int(v).to_bytes(2, byteorder="little", signed=True)
        w.writeframes(bytes(frames))


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
        out.mkdir(parents=True, exist_ok=True)

        os.environ["APP_ROOT"] = str(root)
        os.environ["DUBBING_OUTPUT_DIR"] = str(out)
        os.environ["COOKIE_SECURE"] = "0"
        os.environ["STRICT_SECRETS"] = "0"
        os.environ["REMOTE_ACCESS_MODE"] = "off"

        from dubbing_pipeline.api.models import AuthStore, Role, User, now_ts
        from dubbing_pipeline.api.routes_auth import router as auth_router
        from dubbing_pipeline.jobs.models import Job, JobState, Visibility, now_utc
        from dubbing_pipeline.jobs.store import JobStore
        from dubbing_pipeline.utils.crypto import PasswordHasher
        from dubbing_pipeline.web.routes_jobs import router as jobs_router

        app = FastAPI()
        state_root = (out / "_state").resolve()
        state_root.mkdir(parents=True, exist_ok=True)
        job_store = JobStore(state_root / "jobs.db")
        auth_store = AuthStore(state_root / "auth.db")
        app.state.job_store = job_store
        app.state.auth_store = auth_store
        app.include_router(auth_router)
        app.include_router(jobs_router)

        ph = PasswordHasher()
        admin = User(
            id="u_admin",
            username="admin",
            password_hash=ph.hash("adminpass"),
            role=Role.admin,
            totp_secret=None,
            totp_enabled=False,
            created_at=now_ts(),
        )
        owner = User(
            id="u_owner",
            username="owner",
            password_hash=ph.hash("ownerpass"),
            role=Role.editor,
            totp_secret=None,
            totp_enabled=False,
            created_at=now_ts(),
        )
        viewer = User(
            id="u_viewer",
            username="viewer",
            password_hash=ph.hash("viewerpass"),
            role=Role.viewer,
            totp_secret=None,
            totp_enabled=False,
            created_at=now_ts(),
        )
        auth_store.upsert_user(admin)
        auth_store.upsert_user(owner)
        auth_store.upsert_user(viewer)

        # Seed a job + library record for series access.
        jid = "job_test_voice"
        series_slug = "tensura"
        j = Job(
            id=jid,
            owner_id=owner.id,
            video_path=str(root / "Input" / "dummy.mp4"),
            duration_s=1.0,
            src_lang="ja",
            tgt_lang="en",
            device="cpu",
            mode="high",
            state=JobState.DONE,
            progress=1.0,
            message="ok",
            created_at=now_utc(),
            updated_at=now_utc(),
            output_mkv="",
            output_srt="",
            work_dir="",
            log_path="",
            series_title="That Time I Got Reincarnated as a Slime",
            series_slug=series_slug,
            season_number=1,
            episode_number=1,
            visibility=Visibility.public,
            runtime={},
        )
        job_store.put(j)

        # Seed job-local speaker ref manifest + wav.
        base = out / Path(j.video_path).stem
        voice_refs_dir = (base / "analysis" / "voice_refs").resolve()
        voice_refs_dir.mkdir(parents=True, exist_ok=True)
        ref_wav = (voice_refs_dir / "SPEAKER_01.wav").resolve()
        _write_tone_wav(ref_wav)
        manifest = {
            "items": {
                "SPEAKER_01": {
                    "ref_path": str(ref_wav),
                    "job_ref_path": str(ref_wav),
                    "duration_s": 0.3,
                    "target_s": 30.0,
                    "warnings": [],
                }
            }
        }
        import json

        (voice_refs_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        with TestClient(app) as c:
            csrf_owner = _login(c, "owner", "ownerpass")

            # Create character
            r = c.post(
                f"/api/series/{series_slug}/characters",
                json={"display_name": "Rimuru"},
                headers={"x-csrf-token": csrf_owner},
            )
            assert r.status_code == 200, r.text
            char_slug = r.json()["character"]["character_slug"]
            assert char_slug == "rimuru"

            # Save mapping
            r = c.post(
                f"/api/jobs/{jid}/speaker-mapping",
                json={"speaker_id": "SPEAKER_01", "character_slug": char_slug, "locked": True},
                headers={"x-csrf-token": csrf_owner},
            )
            assert r.status_code == 200, r.text

            # Promote ref
            r = c.post(
                f"/api/series/{series_slug}/characters/{char_slug}/promote-ref",
                json={"job_id": jid, "speaker_id": "SPEAKER_01"},
                headers={"x-csrf-token": csrf_owner},
            )
            assert r.status_code == 200, r.text

            # List characters (should include audio_url)
            r = c.get(f"/api/series/{series_slug}/characters")
            assert r.status_code == 200, r.text
            items = r.json().get("items") or []
            assert any(it.get("character_slug") == char_slug and it.get("audio_url") for it in items)

            # Character audio fetch works for owner.
            r = c.get(f"/api/series/{series_slug}/characters/{char_slug}/audio")
            assert r.status_code == 200, r.text
            assert r.headers.get("content-type", "").startswith("audio/")

            # Viewer cannot edit (403/404).
            c2 = TestClient(app)
            try:
                csrf_viewer = _login(c2, "viewer", "viewerpass")
                r = c2.post(
                    f"/api/series/{series_slug}/characters",
                    json={"display_name": "Shion"},
                    headers={"x-csrf-token": csrf_viewer},
                )
                assert r.status_code in (403, 404), r.text
            finally:
                c2.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

