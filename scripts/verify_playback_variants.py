from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient


def _login(c: TestClient, username: str, password: str) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": username, "password": password, "session": True})
    assert r.status_code == 200, r.text
    d = r.json()
    return {"X-CSRF-Token": d["csrf_token"]}


def main() -> int:
    out = Path("/tmp/anime_v2_verify_playback").resolve()
    out.mkdir(parents=True, exist_ok=True)
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(out)
    os.environ["ANIME_V2_LOG_DIR"] = str(out / "logs")
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"

    from anime_v2.config import get_settings
    from anime_v2.jobs.models import Job, JobState
    from anime_v2.server import app

    get_settings.cache_clear()

    job_id = "j_playback_1"
    base_dir = out / "Sample"
    (base_dir / "mobile").mkdir(parents=True, exist_ok=True)
    (base_dir / "mobile" / "hls").mkdir(parents=True, exist_ok=True)
    (base_dir / "audio" / "tracks").mkdir(parents=True, exist_ok=True)
    (base_dir / "subs").mkdir(parents=True, exist_ok=True)

    # Dummy artifacts
    (base_dir / "Sample.dub.mkv").write_bytes(b"mkv")
    (base_dir / "Sample.dub.mp4").write_bytes(b"mp4")
    (base_dir / "mobile" / "mobile.mp4").write_bytes(b"mp4m")
    (base_dir / "mobile" / "original.mp4").write_bytes(b"mp4o")
    (base_dir / "mobile" / "hls" / "index.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    (base_dir / "audio" / "tracks" / "original_full.m4a").write_bytes(b"m4a")
    (base_dir / "subs" / "tgt_literal.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n\n", encoding="utf-8")

    with TestClient(app) as c:
        # seed job
        c.app.state.job_store.put(
            Job(
                id=job_id,
                owner_id="u1",
                video_path="/workspace/Input/Test.mp4",
                duration_s=1.0,
                mode="low",
                device="cpu",
                src_lang="ja",
                tgt_lang="en",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                state=JobState.DONE,
                progress=1.0,
                message="Done",
                output_mkv=str(base_dir / "Sample.dub.mkv"),
                output_srt=str(base_dir / "tgt_literal.srt"),
                work_dir=str(base_dir),
                log_path=str(base_dir / "job.log"),
            )
        )

        hdr = _login(c, "admin", "adminpass")
        r = c.get(f"/api/jobs/{job_id}/files", headers=hdr)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("mobile_mp4") and data["mobile_mp4"]["url"]
        assert data.get("mobile_original_mp4") and data["mobile_original_mp4"]["url"]
        assert data.get("mobile_hls_manifest") and data["mobile_hls_manifest"]["url"]
        # download tracks are included in files list
        kinds = [f.get("kind") for f in (data.get("files") or []) if isinstance(f, dict)]
        assert "audio_track" in kinds
        assert "subs" in kinds

    print("verify_playback_variants: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

