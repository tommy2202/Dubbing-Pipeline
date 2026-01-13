from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient


def _login(c: TestClient, username: str, password: str) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": username, "password": password, "session": True})
    assert r.status_code == 200, r.text
    d = r.json()
    return {"X-CSRF-Token": d["csrf_token"]}


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        out = (root / "Output").resolve()
        inp = (root / "Input").resolve()
        out.mkdir(parents=True, exist_ok=True)
        inp.mkdir(parents=True, exist_ok=True)
        (inp / "Test.mp4").write_bytes(b"\x00" * 1024)

        os.environ["APP_ROOT"] = str(root)
        os.environ["INPUT_DIR"] = str(inp)
        os.environ["ANIME_V2_OUTPUT_DIR"] = str(out)
        os.environ["ANIME_V2_LOG_DIR"] = str(out / "logs")
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "adminpass"
        os.environ["COOKIE_SECURE"] = "0"

        from anime_v2.config import get_settings
        from anime_v2.jobs.models import Job, JobState
        from anime_v2.server import app

        get_settings.cache_clear()

        job_id = "j_lib_1"
        base_dir = out / "Sample"
        base_dir.mkdir(parents=True, exist_ok=True)
        (out / "jobs" / job_id).mkdir(parents=True, exist_ok=True)

        with TestClient(app) as c:
            c.app.state.job_store.put(
                Job(
                    id=job_id,
                    owner_id="u1",
                    video_path=str(inp / "Test.mp4"),
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
                    output_srt="",
                    work_dir=str(base_dir),
                    log_path=str(base_dir / "job.log"),
                    runtime={"project_name": "projA", "tags": ["tag1"], "archived": True},
                )
            )

            hdr = _login(c, "admin", "adminpass")

            # Archived hidden by default
            r = c.get("/api/jobs?limit=50", headers=hdr)
            assert r.status_code == 200
            ids = [it["id"] for it in r.json().get("items", [])]
            assert job_id not in ids

            # Include archived + tag filter finds it
            r2 = c.get("/api/jobs?include_archived=1&tag=tag1&project=projA", headers=hdr)
            assert r2.status_code == 200
            ids2 = [it["id"] for it in r2.json().get("items", [])]
            assert job_id in ids2

            # Unarchive
            r3 = c.post(f"/api/jobs/{job_id}/unarchive", headers=hdr)
            assert r3.status_code == 200

            # Set tags
            r4 = c.put(f"/api/jobs/{job_id}/tags", json={"tags": ["tag2", "tag3"]}, headers=hdr)
            assert r4.status_code == 200, r4.text

            # Delete removes record and best-effort removes dirs
            r5 = c.delete(f"/api/jobs/{job_id}", headers=hdr)
            assert r5.status_code == 200, r5.text
            assert c.app.state.job_store.get(job_id) is None

    print("verify_library_ops: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

