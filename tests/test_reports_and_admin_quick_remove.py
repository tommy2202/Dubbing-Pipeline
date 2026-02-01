from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.api.models import Role, User, now_ts
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState, Visibility, now_utc
from dubbing_pipeline.notify import ntfy
from dubbing_pipeline.server import app
from dubbing_pipeline.utils.crypto import PasswordHasher, random_id
from tests._helpers.auth import login_user


def _runtime_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
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
    return in_dir, out_dir, logs_dir


def _make_user(*, username: str, password: str, role: Role) -> User:
    ph = PasswordHasher()
    return User(
        id=random_id("u_", 16),
        username=username,
        password_hash=ph.hash(password),
        role=role,
        totp_secret=None,
        totp_enabled=False,
        created_at=now_ts(),
    )


def test_reports_and_admin_quick_remove(tmp_path: Path, monkeypatch) -> None:
    _runtime_paths(tmp_path)
    os.environ["NTFY_ENABLED"] = "1"
    os.environ["NTFY_ADMIN_TOPIC"] = "admin_reports"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["MIN_FREE_GB"] = "0"
    get_settings.cache_clear()

    notify_calls: list[dict[str, str]] = []

    def _fake_notify(**kwargs):
        notify_calls.append({k: str(v) for k, v in kwargs.items()})
        return True

    monkeypatch.setattr(ntfy, "notify", _fake_notify)

    with TestClient(app) as c:
        auth = c.app.state.auth_store
        store = c.app.state.job_store
        user_a = _make_user(username="report_owner", password="pass_a", role=Role.operator)
        user_b = _make_user(username="report_viewer", password="pass_b", role=Role.viewer)
        admin = _make_user(username="report_admin", password="adminpass", role=Role.admin)
        auth.upsert_user(user_a)
        auth.upsert_user(user_b)
        auth.upsert_user(admin)

        headers_a = login_user(c, username="report_owner", password="pass_a", clear_cookies=True)
        headers_b = login_user(c, username="report_viewer", password="pass_b", clear_cookies=True)
        headers_admin = login_user(
            c, username="report_admin", password="adminpass", clear_cookies=True
        )

        job = Job(
            id="job_report_1",
            owner_id=user_a.id,
            video_path=str(Path(os.environ["INPUT_DIR"]) / "src.mp4"),
            duration_s=1.0,
            mode="low",
            device="cpu",
            src_lang="ja",
            tgt_lang="en",
            created_at=now_utc(),
            updated_at=now_utc(),
            state=JobState.DONE,
            progress=1.0,
            message="done",
            output_mkv="",
            output_srt="",
            work_dir="",
            log_path="",
            error=None,
            series_title="Series R",
            series_slug="series-r",
            season_number=1,
            episode_number=1,
            visibility=Visibility.shared,
        )
        store.put(job)

        series_before = c.get("/api/library/series", headers=headers_b)
        assert series_before.status_code == 200
        assert any(it.get("series_slug") == "series-r" for it in series_before.json())

        key = "series-r:1:1"
        r = c.post(
            f"/api/library/{key}/report",
            headers=headers_b,
            json={"reason": "spam"},
        )
        assert r.status_code == 200, r.text
        report_id = str(r.json().get("report_id") or "")
        assert report_id
        assert notify_calls

        reports = store.list_library_reports(limit=50, offset=0, status="open")
        assert any(it.get("id") == report_id for it in reports)

        r2 = c.post(
            f"/api/library/{key}/admin_remove",
            headers=headers_admin,
            json={"delete": False},
        )
        assert r2.status_code == 200, r2.text

        series_after = c.get("/api/library/series", headers=headers_b)
        assert series_after.status_code == 200
        assert not any(it.get("series_slug") == "series-r" for it in series_after.json())
