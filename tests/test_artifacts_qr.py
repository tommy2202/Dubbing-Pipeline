from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState
from dubbing_pipeline.server import app


def _runtime_video_path(tmp_path: Path) -> str:
    root = tmp_path.resolve()
    in_dir = root / "Input"
    out_dir = root / "Output"
    logs_dir = root / "logs"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    vp = in_dir / "Test.mp4"
    if not vp.exists():
        vp.write_bytes(b"\x00" * 1024)
    os.environ["APP_ROOT"] = str(root)
    os.environ["INPUT_DIR"] = str(in_dir)
    os.environ["DUBBING_OUTPUT_DIR"] = str(out_dir)
    os.environ["DUBBING_LOG_DIR"] = str(logs_dir)
    return str(vp)


def _login_admin(c: TestClient) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    assert r.status_code == 200
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def test_job_files_and_qrcode(tmp_path: Path) -> None:
    video_path = _runtime_video_path(tmp_path)
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
                video_path=video_path,
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
