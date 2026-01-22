from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

try:
    from fastapi.testclient import TestClient
except Exception as ex:  # pragma: no cover - optional deps
    print(f"verify_voice_mapping: SKIP (fastapi unavailable: {ex})")
    raise SystemExit(0)

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


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
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

        with TestClient(app) as c:
            store = c.app.state.job_store
            auth = c.app.state.auth_store
            ph = PasswordHasher()

            owner = User(
                id=random_id("u_", 16),
                username="owner",
                password_hash=ph.hash("ownerpass"),
                role=Role.operator,
                totp_secret=None,
                totp_enabled=False,
                created_at=now_ts(),
            )
            auth.upsert_user(owner)
            headers_owner = _login(c, username="owner", password="ownerpass")

            job_id = "job_voice_map_verify"
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
                owner_id=owner.id,
                video_path=str(in_dir / "source.mp4"),
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

            r = c.get(f"/api/jobs/{job_id}/speakers", headers=headers_owner)
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["items"][0]["speaker_id"] == "SPEAKER_01"

            r = c.post(
                f"/api/jobs/{job_id}/voice-mapping",
                headers=headers_owner,
                json={"items": [{"speaker_id": "SPEAKER_01", "strategy": "original"}]},
            )
            assert r.status_code == 200, r.text

            r = c.get(f"/api/jobs/{job_id}/speakers", headers=headers_owner)
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["items"][0]["mapping"]["strategy"] == "original"

    print("verify_voice_mapping: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
