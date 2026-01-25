from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.runtime import lifecycle
from dubbing_pipeline.server import app
from tests._helpers.auth import login_admin
from tests._helpers.media import ensure_tiny_mp4


def run_smoke_fresh_machine(
    root: Path, *, ffmpeg_skip_message: str | None = None, timeout_s: int = 180
) -> None:
    root = root.resolve()
    in_dir = root / "Input"
    out_dir = root / "Output"
    logs_dir = root / "logs"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    mp4_path = ensure_tiny_mp4(
        in_dir / "Test.mp4",
        duration_s=1.2,
        skip_message=ffmpeg_skip_message,
    )

    os.environ["APP_ROOT"] = str(root)
    os.environ["INPUT_DIR"] = str(in_dir)
    os.environ["DUBBING_OUTPUT_DIR"] = str(out_dir)
    os.environ["DUBBING_LOG_DIR"] = str(logs_dir)
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()
    lifecycle.end_draining()

    with TestClient(app) as client:
        headers = login_admin(client)
        resp = client.post(
            "/api/jobs",
            headers=headers,
            json={
                "video_path": str(mp4_path),
                "mode": "low",
                "device": "cpu",
                "series_title": "Smoke Series",
                "season_number": 1,
                "episode_number": 1,
            },
        )
        assert resp.status_code == 200
        job_id = resp.json()["id"]

        deadline = time.monotonic() + float(timeout_s)
        job = None
        while time.monotonic() < deadline:
            jr = client.get(f"/api/jobs/{job_id}", headers=headers)
            assert jr.status_code == 200
            job = jr.json()
            state = str(job.get("state") or "")
            if state in {"DONE", "FAILED", "CANCELED"}:
                break
            time.sleep(1.0)
        assert job is not None
        assert str(job.get("state") or "") == "DONE"
        assert str(job.get("visibility") or "") == "private"

        files = client.get(f"/api/jobs/{job_id}/files", headers=headers)
        assert files.status_code == 200
        data = files.json()
        output_path = None
        for key in ("mkv", "mp4", "mobile_mp4", "mobile_original_mp4", "lipsync_mp4"):
            ent = data.get(key)
            if isinstance(ent, dict) and ent.get("path"):
                output_path = Path(str(ent["path"]))
                break
        assert output_path is not None and output_path.exists()

        base_dir = output_path.parent
        manifest = base_dir / "manifest.json"
        assert manifest.exists()
