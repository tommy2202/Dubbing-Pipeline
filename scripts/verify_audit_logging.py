from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState, now_utc
from dubbing_pipeline.server import app


def _set_env(root: Path) -> Path:
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
    get_settings.cache_clear()
    return logs_dir


def _read_audit(log_dir: Path) -> list[dict]:
    path = log_dir / "audit.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _rid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="audit-verify-") as td:
        root = Path(td)
        log_dir = _set_env(root)
        with TestClient(app) as client:
            rid_login = _rid("login")
            resp = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "adminpass"},
                headers={"X-Request-ID": rid_login},
            )
            if resp.status_code != 200:
                print("login failed", resp.status_code, resp.text)
                return 1
            payload = resp.json()
            access = payload["access_token"]
            csrf = payload["csrf_token"]
            headers = {"Authorization": f"Bearer {access}", "X-CSRF-Token": csrf}

            rid_refresh = _rid("refresh")
            client.post("/api/auth/refresh", headers={**headers, "X-Request-ID": rid_refresh})

            rid_upload = _rid("upload")
            client.post(
                "/api/uploads/init",
                json={"filename": "sample.mp4", "total_bytes": 4, "mime": "video/mp4"},
                headers={**headers, "X-Request-ID": rid_upload},
            )

            rid_admin = _rid("admin")
            client.get("/api/admin/queue", headers={**headers, "X-Request-ID": rid_admin})

            auth_store = client.app.state.auth_store
            user = auth_store.get_user_by_username("admin")
            if user is None:
                print("admin user missing")
                return 1
            job_id = f"job_{uuid.uuid4().hex[:8]}"
            now = now_utc()
            out_dir = Path(os.environ["DUBBING_OUTPUT_DIR"]) / "jobs" / job_id
            out_dir.mkdir(parents=True, exist_ok=True)
            artifact = out_dir / "artifact.txt"
            artifact.write_text("data", encoding="utf-8")
            job = Job(
                id=job_id,
                owner_id=str(user.id),
                video_path="",
                duration_s=0.0,
                mode="fast",
                device="cpu",
                src_lang="en",
                tgt_lang="en",
                created_at=now,
                updated_at=now,
                state=JobState.QUEUED,
                progress=0.0,
                message="",
                output_mkv=str(artifact),
                output_srt="",
                work_dir=str(out_dir),
                log_path=str(out_dir / "job.log"),
            )
            client.app.state.job_store.put(job)

            rid_download = _rid("download")
            client.get(
                f"/files/jobs/{job_id}/artifact.txt",
                headers={**headers, "X-Request-ID": rid_download},
            )

        rows = _read_audit(log_dir)
        interesting = {
            "auth.login_ok",
            "auth.refresh_ok",
            "upload.init",
            "admin.queue_view",
            "file.download",
        }
        print("audit entries")
        for rec in rows:
            if rec.get("event") in interesting:
                print(json.dumps(rec, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
