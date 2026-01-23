from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient


def _login(c: TestClient, *, username: str, password: str) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def main() -> int:
    from dubbing_pipeline.config import get_settings
    from dubbing_pipeline.jobs.models import Job, JobState
    from dubbing_pipeline.server import app

    with tempfile.TemporaryDirectory(prefix="verify_streaming_range_") as td:
        root = Path(td)
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
        os.environ["MIN_FREE_GB"] = "0"

        get_settings.cache_clear()

        with TestClient(app) as c:
            headers = _login(c, username="admin", password="adminpass")
            store = c.app.state.job_store
            auth = c.app.state.auth_store
            admin = auth.get_user_by_username("admin")
            assert admin is not None

            job_dir = out_dir / "range_job"
            job_dir.mkdir(parents=True, exist_ok=True)
            payload = bytes(range(64))
            media_path = job_dir / "range_job.dub.mp4"
            media_path.write_bytes(payload)

            job = Job(
                id="range_job",
                owner_id=admin.id,
                video_path=str((in_dir / "source.mp4").resolve()),
                duration_s=1.0,
                mode="low",
                device="cpu",
                src_lang="ja",
                tgt_lang="en",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                state=JobState.DONE,
                progress=1.0,
                message="done",
                output_mkv=str(media_path),
                output_srt="",
                work_dir=str(job_dir),
                log_path=str(job_dir / "job.log"),
            )
            store.put(job)

            rel = media_path.relative_to(out_dir).as_posix()

            r = c.get(f"/files/{rel}", headers={**headers, "Range": "bytes=0-9"})
            assert r.status_code == 206, r.text
            assert r.headers.get("content-range") == "bytes 0-9/64"
            assert r.headers.get("content-length") == "10"
            assert len(r.content) == 10

            r2 = c.get(f"/files/{rel}", headers=headers)
            assert r2.status_code == 200, r2.text
            assert r2.headers.get("content-length") == "64"
            assert len(r2.content) == 64

        print("verify_streaming_range: PASS")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
