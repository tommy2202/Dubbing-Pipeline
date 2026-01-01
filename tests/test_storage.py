from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from anime_v2.config import get_settings
from anime_v2.ops.storage import ensure_free_space, prune_stale_workdirs
from anime_v2.server import app


def test_ensure_free_space_raises_507(tmp_path: Path) -> None:
    with pytest.raises(HTTPException) as ex:
        ensure_free_space(min_gb=10**9, path=tmp_path)
    assert ex.value.status_code == 507


def test_prune_stale_workdirs_removes_old_dirs(tmp_path: Path) -> None:
    out = tmp_path / "Output"
    stale = out / "Show1" / "work" / "oldjob"
    fresh = out / "Show1" / "work" / "newjob"
    stale.mkdir(parents=True, exist_ok=True)
    fresh.mkdir(parents=True, exist_ok=True)

    old_ts = time.time() - (48 * 3600)
    os.utime(stale, (old_ts, old_ts))

    removed = prune_stale_workdirs(output_root=out, max_age_hours=24)
    assert removed >= 1
    assert not stale.exists()
    assert fresh.exists()


def test_submit_job_returns_507_when_disk_guard_trips(tmp_path: Path) -> None:
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["MIN_FREE_GB"] = str(10**9)
    get_settings.cache_clear()

    with TestClient(app) as c:
        r = c.post("/auth/login", json={"username": "admin", "password": "adminpass"})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}", "X-CSRF-Token": r.json()["csrf_token"]}
        r2 = c.post("/api/jobs", headers=headers, json={"video_path": "/workspace/Input/Test.mp4", "device": "cpu", "mode": "low"})
        assert r2.status_code == 507

