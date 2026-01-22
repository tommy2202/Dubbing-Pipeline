from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.api.models import Role, User, now_ts
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState, Visibility
from dubbing_pipeline.server import app
from dubbing_pipeline.utils.crypto import PasswordHasher, random_id


def _login(c: TestClient, *, username: str, password: str) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def test_voice_mapping_endpoints() -> None:
    with TestClient(app) as c:
        store = c.app.state.job_store
        auth = c.app.state.auth_store
        ph = PasswordHasher()

        user_a = User(
            id=random_id("u_", 16),
            username="user_a",
            password_hash=ph.hash("pass_a"),
            role=Role.operator,
            totp_secret=None,
            totp_enabled=False,
            created_at=now_ts(),
        )
        user_b = User(
            id=random_id("u_", 16),
            username="user_b",
            password_hash=ph.hash("pass_b"),
            role=Role.viewer,
            totp_secret=None,
            totp_enabled=False,
            created_at=now_ts(),
        )
        auth.upsert_user(user_a)
        auth.upsert_user(user_b)

        headers_a = _login(c, username="user_a", password="pass_a")
        headers_b = _login(c, username="user_b", password="pass_b")

        out_dir = Path(get_settings().output_dir).resolve()
        job_id = "job_voice_map"
        base_dir = out_dir / job_id
        ref_dir = base_dir / "analysis" / "voice_refs"
        ref_dir.mkdir(parents=True, exist_ok=True)
        ref_path = ref_dir / "SPEAKER_01.wav"
        ref_path.write_bytes(b"\x00" * 16)
        manifest = {
            "items": {
                "SPEAKER_01": {
                    "job_ref_path": str(ref_path),
                    "duration_s": 6.0,
                }
            }
        }
        (ref_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        job = Job(
            id=job_id,
            owner_id=user_a.id,
            video_path=str(out_dir / "source.mp4"),
            duration_s=10.0,
            mode="low",
            device="cpu",
            src_lang="ja",
            tgt_lang="en",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            state=JobState.DONE,
            progress=1.0,
            message="done",
            output_mkv=str(base_dir / f"{job_id}.dub.mkv"),
            output_srt="",
            work_dir=str(base_dir),
            log_path=str(base_dir / "job.log"),
            visibility=Visibility.private,
        )
        store.put(job)

        r = c.get(f"/api/jobs/{job_id}/speakers", headers=headers_a)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["available"] is True
        assert data["items"][0]["speaker_id"] == "SPEAKER_01"
        assert "/api/jobs/" in str(data["items"][0]["audio_url"] or "")

        r = c.get(f"/api/jobs/{job_id}/speakers", headers=headers_b)
        assert r.status_code == 403, r.text

        r = c.post(
            f"/api/jobs/{job_id}/voice-mapping",
            headers=headers_a,
            json={"items": [{"speaker_id": "SPEAKER_01", "strategy": "preset", "preset": "default"}]},
        )
        assert r.status_code == 200, r.text
        job2 = store.get(job_id)
        assert job2 is not None
        vm = (job2.runtime or {}).get("voice_map", [])
        assert isinstance(vm, list)
        assert vm and vm[0].get("speaker_strategy") == "preset"

        r = c.get(f"/api/jobs/{job_id}/speakers", headers=headers_a)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["items"][0]["mapping"]["strategy"] == "preset"

        # Fallback speaker when diarization refs are missing.
        job_id2 = "job_voice_map_fallback"
        job2 = Job(
            id=job_id2,
            owner_id=user_a.id,
            video_path=str(out_dir / "source2.mp4"),
            duration_s=10.0,
            mode="low",
            device="cpu",
            src_lang="ja",
            tgt_lang="en",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            state=JobState.DONE,
            progress=1.0,
            message="done",
            output_mkv=str(out_dir / job_id2 / f"{job_id2}.dub.mkv"),
            output_srt="",
            work_dir=str(out_dir / job_id2),
            log_path=str(out_dir / job_id2 / "job.log"),
            visibility=Visibility.private,
        )
        store.put(job2)

        r = c.get(f"/api/jobs/{job_id2}/speakers", headers=headers_a)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["available"] is False
        assert data["items"][0]["speaker_id"] == "SPEAKER_01"
