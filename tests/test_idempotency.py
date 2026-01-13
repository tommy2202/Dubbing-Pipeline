from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from anime_v2.config import get_settings
from anime_v2.server import app


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
        # Job submission may call ffprobe; generate a real tiny MP4.
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=160x90:rate=10",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t",
                "1.0",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(vp),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    os.environ["APP_ROOT"] = str(root)
    os.environ["INPUT_DIR"] = str(in_dir)
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(out_dir)
    os.environ["ANIME_V2_LOG_DIR"] = str(logs_dir)
    return str(vp)


def test_idempotency_key_returns_same_job_id(tmp_path: Path) -> None:
    video_path = _runtime_video_path(tmp_path)
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        r = c.post("/auth/login", json={"username": "admin", "password": "adminpass"})
        token = r.json()["access_token"]
        headers = {
            "Authorization": f"Bearer {token}",
            "X-CSRF-Token": r.json()["csrf_token"],
            "Idempotency-Key": "abc123",
        }

        r1 = c.post(
            "/api/jobs",
            headers=headers,
            json={"video_path": video_path, "device": "cpu", "mode": "low"},
        )
        r2 = c.post(
            "/api/jobs",
            headers=headers,
            json={"video_path": video_path, "device": "cpu", "mode": "low"},
        )
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["id"] == r2.json()["id"]
