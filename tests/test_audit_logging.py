from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState, now_utc
from dubbing_pipeline.server import app


def _set_env(tmp_path: Path) -> Path:
    root = tmp_path.resolve()
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


def _find_event(rows: list[dict], event: str, request_id: str) -> dict | None:
    for rec in rows:
        if rec.get("event") == event and rec.get("request_id") == request_id:
            return rec
    return None


def _new_rid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def test_audit_logging_request_id_and_actor(tmp_path: Path) -> None:
    log_dir = _set_env(tmp_path)
    with TestClient(app) as client:
        rid_login_fail = _new_rid("login-fail")
        r = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "badpass"},
            headers={"X-Request-ID": rid_login_fail},
        )
        assert r.status_code in {401, 403}

        rid_login_ok = _new_rid("login-ok")
        r = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "adminpass"},
            headers={"X-Request-ID": rid_login_ok},
        )
        assert r.status_code == 200
        payload = r.json()
        access = payload["access_token"]
        csrf = payload["csrf_token"]
        auth_headers = {"Authorization": f"Bearer {access}", "X-CSRF-Token": csrf}

        rid_refresh = _new_rid("refresh")
        r = client.post("/api/auth/refresh", headers={**auth_headers, "X-Request-ID": rid_refresh})
        assert r.status_code == 200

        rid_key = _new_rid("key-create")
        r = client.post(
            "/keys",
            json={"scopes": ["read:job"]},
            headers={**auth_headers, "X-Request-ID": rid_key},
        )
        assert r.status_code == 200

        rid_admin = _new_rid("admin-queue")
        r = client.get("/api/admin/queue", headers={**auth_headers, "X-Request-ID": rid_admin})
        assert r.status_code == 200

        auth_store = client.app.state.auth_store
        user = auth_store.get_user_by_username("admin")
        assert user is not None
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

        rid_upload = _new_rid("upload-init")
        r = client.post(
            "/api/uploads/init",
            json={"filename": "test.mp4", "total_bytes": 4, "mime": "video/mp4"},
            headers={**auth_headers, "X-Request-ID": rid_upload},
        )
        assert r.status_code == 200

        rid_cancel = _new_rid("job-cancel")
        r = client.post(
            f"/api/jobs/{job_id}/cancel",
            headers={**auth_headers, "X-Request-ID": rid_cancel},
        )
        assert r.status_code == 200

        rid_download = _new_rid("file-download")
        r = client.get(
            f"/files/jobs/{job_id}/artifact.txt",
            headers={**auth_headers, "X-Request-ID": rid_download},
        )
        assert r.status_code in {200, 206}

        rid_delete = _new_rid("job-delete")
        r = client.delete(
            f"/api/jobs/{job_id}",
            headers={**auth_headers, "X-Request-ID": rid_delete},
        )
        assert r.status_code == 200

    rows = _read_audit(log_dir)
    assert _find_event(rows, "auth.login_failed", rid_login_fail)
    ok = _find_event(rows, "auth.login_ok", rid_login_ok)
    assert ok and ok.get("user_id")
    ref = _find_event(rows, "auth.refresh_ok", rid_refresh)
    assert ref and ref.get("user_id")
    key = _find_event(rows, "api_key.create", rid_key)
    assert key and key.get("user_id")
    admin = _find_event(rows, "admin.queue_view", rid_admin)
    assert admin and admin.get("user_id")
    upload = _find_event(rows, "upload.init", rid_upload)
    assert upload and upload.get("user_id")
    cancel = _find_event(rows, "job.cancel", rid_cancel)
    assert cancel and cancel.get("user_id")
    download = _find_event(rows, "file.download", rid_download)
    assert download and download.get("user_id")
    delete = _find_event(rows, "job.delete", rid_delete)
    assert delete and delete.get("user_id")
