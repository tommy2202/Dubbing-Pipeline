from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.api.models import Role, User, now_ts
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState, Visibility, now_utc
from dubbing_pipeline.ops import retention
from dubbing_pipeline.server import app
from dubbing_pipeline.utils.crypto import PasswordHasher, random_id


def _runtime_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path.resolve()
    in_dir = root / "Input"
    out_dir = root / "Output"
    logs_dir = root / "logs"
    state_dir = root / "_state"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    os.environ["APP_ROOT"] = str(root)
    os.environ["INPUT_DIR"] = str(in_dir)
    os.environ["DUBBING_OUTPUT_DIR"] = str(out_dir)
    os.environ["DUBBING_LOG_DIR"] = str(logs_dir)
    os.environ["DUBBING_STATE_DIR"] = str(state_dir)
    return in_dir, out_dir, logs_dir


def _login(c: TestClient, *, username: str, password: str) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def _make_job(
    *,
    job_id: str,
    owner_id: str,
    in_dir: Path,
    out_dir: Path,
    updated_at: str,
    runtime: dict | None = None,
    series: bool = False,
) -> Job:
    job_dir = out_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    output_mkv = job_dir / f"{job_id}.dub.mp4"
    output_mkv.write_bytes(b"\x00" * 8)
    log_path = job_dir / "job.log"
    log_path.write_text("log\n", encoding="utf-8")
    return Job(
        id=job_id,
        owner_id=owner_id,
        video_path=str(in_dir / "source.mp4"),
        duration_s=10.0,
        mode="low",
        device="cpu",
        src_lang="ja",
        tgt_lang="en",
        created_at=updated_at,
        updated_at=updated_at,
        state=JobState.DONE,
        progress=1.0,
        message="done",
        output_mkv=str(output_mkv),
        output_srt="",
        work_dir=str(job_dir),
        log_path=str(log_path),
        series_title="Series A" if series else "",
        series_slug="series-a" if series else "",
        season_number=1 if series else 0,
        episode_number=1 if series else 0,
        visibility=Visibility.private,
        runtime=runtime or {},
    )


def test_retention_and_delete(tmp_path: Path) -> None:
    in_dir, out_dir, _logs_dir = _runtime_paths(tmp_path)
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["MIN_FREE_GB"] = "0"
    os.environ["RETENTION_ENABLED"] = "1"
    os.environ["RETENTION_UPLOAD_TTL_HOURS"] = "1"
    os.environ["RETENTION_JOB_ARTIFACT_DAYS"] = "1"
    os.environ["RETENTION_LOG_DAYS"] = "1"
    os.environ["RETENTION_INTERVAL_SEC"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        store = c.app.state.job_store
        auth = c.app.state.auth_store
        ph = PasswordHasher()

        user_a = User(
            id=random_id("u_", 16),
            username="user_a",
            password_hash=ph.hash("pass_a"),
            role=Role.operator,
            totp_secret=None,
            totp_enabled=False,
            created_at=now_ts(),
        )
        user_b = User(
            id=random_id("u_", 16),
            username="user_b",
            password_hash=ph.hash("pass_b"),
            role=Role.viewer,
            totp_secret=None,
            totp_enabled=False,
            created_at=now_ts(),
        )
        auth.upsert_user(user_a)
        auth.upsert_user(user_b)

        headers_b = _login(c, username="user_b", password="pass_b")
        headers_a = _login(c, username="user_a", password="pass_a")

        old_ts = "2024-01-01T00:00:00+00:00"
        job_old = _make_job(
            job_id="job_retention_old",
            owner_id=user_a.id,
            in_dir=in_dir,
            out_dir=out_dir,
            updated_at=old_ts,
        )
        job_pinned = _make_job(
            job_id="job_retention_pinned",
            owner_id=user_a.id,
            in_dir=in_dir,
            out_dir=out_dir,
            updated_at=old_ts,
            runtime={"pinned": True},
        )
        store.put(job_old)
        store.put(job_pinned)

        uploads_dir = (in_dir / "uploads").resolve()
        uploads_dir.mkdir(parents=True, exist_ok=True)
        upload_id = "up_retention_1"
        part_path = uploads_dir / f"{upload_id}.part"
        part_path.write_bytes(b"\x01" * 4)
        store.put_upload(
            upload_id,
            {
                "id": upload_id,
                "owner_id": user_a.id,
                "filename": "x.mp4",
                "total_bytes": 4,
                "chunk_bytes": 4,
                "received": {},
                "received_bytes": 0,
                "completed": False,
                "part_path": str(part_path),
                "final_path": str(uploads_dir / f"{upload_id}_x.mp4"),
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-01T00:00:00+00:00",
            },
        )

        res = retention.run_once(store=store, output_root=out_dir, app_root=tmp_path)
        assert res.uploads_removed >= 1
        assert res.jobs_removed >= 1
        assert store.get("job_retention_old") is None
        assert store.get("job_retention_pinned") is not None
        assert store.get_upload(upload_id) is None
        assert not part_path.exists()

        job_delete = _make_job(
            job_id="job_delete_owner",
            owner_id=user_a.id,
            in_dir=in_dir,
            out_dir=out_dir,
            updated_at=now_utc(),
        )
        store.put(job_delete)
        c.cookies.set("csrf", headers_b["X-CSRF-Token"])
        r_forbidden = c.delete(f"/api/jobs/{job_delete.id}", headers=headers_b)
        assert r_forbidden.status_code == 403
        c.cookies.set("csrf", headers_a["X-CSRF-Token"])
        r_ok = c.delete(f"/api/jobs/{job_delete.id}", headers=headers_a)
        assert r_ok.status_code == 200
        assert store.get(job_delete.id) is None

        job_lib = _make_job(
            job_id="job_delete_library",
            owner_id=user_a.id,
            in_dir=in_dir,
            out_dir=out_dir,
            updated_at=now_utc(),
            series=True,
        )
        store.put(job_lib)
        r_lib = c.delete("/api/library/series-a/1/1", headers=headers_a)
        assert r_lib.status_code == 200, r_lib.text
        assert store.get(job_lib.id) is None
