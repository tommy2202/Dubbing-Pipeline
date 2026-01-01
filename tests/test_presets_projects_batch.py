from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from anime_v2.config import get_settings
from anime_v2.server import app
from anime_v2.runtime import lifecycle


def _login_admin(c: TestClient) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    assert r.status_code == 200
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def test_presets_projects_and_batch(tmp_path: Path) -> None:
    lifecycle.end_draining()
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    # Use existing real sample (ffprobe must succeed).
    os.environ["APP_ROOT"] = "/workspace"

    with TestClient(app) as c:
        headers = _login_admin(c)
        # preset
        pr = c.post(
            "/api/presets",
            headers=headers,
            json={"name": "p1", "mode": "low", "device": "cpu", "src_lang": "ja", "tgt_lang": "en", "tts_lang": "en", "tts_speaker": "default"},
        )
        assert pr.status_code == 200
        preset_id = pr.json()["id"]
        # project
        pj = c.post(
            "/api/projects",
            headers=headers,
            json={"name": "My Series S01", "default_preset_id": preset_id, "output_subdir": "My Series S01"},
        )
        assert pj.status_code == 200
        project_id = pj.json()["id"]

        # batch (JSON paths)
        items = [{"video_path": "/workspace/Input/Test.mp4", "preset_id": preset_id, "project_id": project_id} for _ in range(3)]
        br = c.post("/api/jobs/batch", headers=headers, json={"items": items})
        assert br.status_code == 200
        ids = br.json()["ids"]
        assert len(ids) == 3

        # confirm jobs exist
        lr = c.get("/api/jobs?limit=10", headers=headers)
        assert lr.status_code == 200
        got = {j["id"] for j in lr.json()["items"]}
        assert set(ids).issubset(got)

