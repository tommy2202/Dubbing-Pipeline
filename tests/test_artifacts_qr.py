from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from anime_v2.config import get_settings
from anime_v2.jobs.models import Job, JobState
from anime_v2.server import app


def _login_admin(c: TestClient) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    assert r.status_code == 200
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def test_job_files_and_qrcode(tmp_path: Path) -> None:
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    out_dir = tmp_path / "Output" / "Test"
    out_dir.mkdir(parents=True, exist_ok=True)
    mp4 = out_dir / "Test.dub.mp4"
    mp4.write_bytes(b"fake")

    with TestClient(app) as c:
        headers = _login_admin(c)
        store = c.app.state.job_store
        now = "2026-01-01T00:00:00+00:00"
        store.put(
            Job(
                id="j_art_1",
                owner_id="u1",
                video_path="/workspace/Input/Test.mp4",
                duration_s=1.0,
                mode="low",
                device="cpu",
                src_lang="ja",
                tgt_lang="en",
                created_at=now,
                updated_at=now,
                state=JobState.DONE,
                progress=1.0,
                message="Done",
                output_mkv="",
                output_srt="",
                work_dir=str(out_dir),
                log_path=str(out_dir / "job.log"),
            )
        )

        r1 = c.get("/api/jobs/j_art_1/files", headers=headers)
        assert r1.status_code == 200
        data = r1.json()
        assert data["mp4"]["url"].startswith("/files/")

        # file serving
        r2 = c.get(data["mp4"]["url"], headers=headers)
        assert r2.status_code == 206

        # QR code
        qr = c.get("/api/jobs/j_art_1/qrcode", headers=headers)
        assert qr.status_code == 200
        assert qr.headers.get("content-type") == "image/png"

