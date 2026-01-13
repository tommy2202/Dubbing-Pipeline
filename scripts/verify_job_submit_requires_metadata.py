from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient


def _make_tiny_mp4(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Deterministic tiny mp4 so ffprobe validation passes.
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x120:rate=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-t",
        "1.0",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        out_dir = (root / "Output").resolve()
        in_dir = (root / "Input").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        in_dir.mkdir(parents=True, exist_ok=True)

        # Configure environment BEFORE importing the app/settings.
        os.environ.setdefault("APP_ROOT", str(root))
        os.environ.setdefault("OUTPUT_DIR", str(out_dir))
        os.environ.setdefault("REMOTE_ACCESS_MODE", "off")
        os.environ.setdefault("COOKIE_SECURE", "0")
        os.environ.setdefault("ADMIN_USERNAME", "admin")
        os.environ.setdefault("ADMIN_PASSWORD", "password123")

        # Clear cached settings (important when running multiple verifies in same interpreter).
        try:
            from config import settings as cfg_settings

            cfg_settings.get_settings.cache_clear()
        except Exception:
            pass

        # Create a valid input video under INPUT_DIR
        vid = in_dir / "Test.mp4"
        _make_tiny_mp4(vid)

        from dubbing_pipeline.server import app

        with TestClient(app) as c:
            # login (cookie session)
            r = c.post(
                "/auth/login",
                json={"username": "admin", "password": "password123", "session": True},
            )
            assert r.status_code == 200, r.text
            csrf = c.cookies.get("csrf") or ""
            assert csrf, "missing csrf cookie"

            headers = {"content-type": "application/json", "X-CSRF-Token": csrf}

            # Missing fields => 422
            r = c.post(
                "/api/jobs",
                headers=headers,
                json={"video_path": "Input/Test.mp4", "mode": "low"},
            )
            assert r.status_code == 422, r.text
            detail = (r.json() or {}).get("detail")
            assert isinstance(detail, str) and "series_title" in detail, detail

            # Invalid season => 422 with helpful message
            r = c.post(
                "/api/jobs",
                headers=headers,
                json={
                    "video_path": "Input/Test.mp4",
                    "series_title": "My Show",
                    "season_text": "Season X",
                    "episode_text": "E4",
                    "mode": "low",
                },
            )
            assert r.status_code == 422, r.text
            detail = (r.json() or {}).get("detail")
            assert isinstance(detail, str) and "season_number" in detail, detail

            # Invalid episode => 422 with helpful message
            r = c.post(
                "/api/jobs",
                headers=headers,
                json={
                    "video_path": "Input/Test.mp4",
                    "series_title": "My Show",
                    "season_text": "S1",
                    "episode_text": "Episode -1",
                    "mode": "low",
                },
            )
            assert r.status_code == 422, r.text
            detail = (r.json() or {}).get("detail")
            assert isinstance(detail, str) and "episode_number" in detail, detail

            # Valid submission => 200
            r = c.post(
                "/api/jobs",
                headers=headers,
                json={
                    "video_path": "Input/Test.mp4",
                    "series_title": "My Show",
                    "season_text": "Season 01",
                    "episode_text": "E04",
                    "mode": "low",
                },
            )
            assert r.status_code == 200, r.text
            jid = (r.json() or {}).get("id")
            assert isinstance(jid, str) and jid, jid

        # Ensure job is stored with normalized metadata.
        store = getattr(app.state, "job_store", None)
        assert store is not None
        job = store.get(jid)
        assert job is not None
        assert job.series_title == "My Show"
        assert job.series_slug == "my-show"
        assert int(job.season_number) == 1
        assert int(job.episode_number) == 4

    print("verify_job_submit_requires_metadata: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

