from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from anime_v2.config import get_settings
from anime_v2.runtime import lifecycle
from anime_v2.server import app


def _runtime_video_path(tmp_path: Path) -> str:
    """
    This test exercises submit paths that may call ffprobe, so we generate a real tiny MP4.
    """
    root = tmp_path.resolve()
    in_dir = root / "Input"
    out_dir = root / "Output"
    logs_dir = root / "logs"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    vp = in_dir / "Test.mp4"
    if not vp.exists():
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


def _login_admin(c: TestClient) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    assert r.status_code == 200
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def test_presets_projects_and_batch(tmp_path: Path) -> None:
    lifecycle.end_draining()
    video_path = _runtime_video_path(tmp_path)
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        headers = _login_admin(c)
        # preset
        pr = c.post(
            "/api/presets",
            headers=headers,
            json={
                "name": "p1",
                "mode": "low",
                "device": "cpu",
                "src_lang": "ja",
                "tgt_lang": "en",
                "tts_lang": "en",
                "tts_speaker": "default",
            },
        )
        assert pr.status_code == 200
        preset_id = pr.json()["id"]
        # project
        pj = c.post(
            "/api/projects",
            headers=headers,
            json={
                "name": "My Series S01",
                "default_preset_id": preset_id,
                "output_subdir": "My Series S01",
            },
        )
        assert pj.status_code == 200
        project_id = pj.json()["id"]

        # batch (JSON paths)
        items = [
            {
                "video_path": video_path,
                "preset_id": preset_id,
                "project_id": project_id,
            }
            for _ in range(3)
        ]
        br = c.post("/api/jobs/batch", headers=headers, json={"items": items})
        assert br.status_code == 200
        ids = br.json()["ids"]
        assert len(ids) == 3

        # confirm jobs exist
        lr = c.get("/api/jobs?limit=10", headers=headers)
        assert lr.status_code == 200
        got = {j["id"] for j in lr.json()["items"]}
        assert set(ids).issubset(got)
