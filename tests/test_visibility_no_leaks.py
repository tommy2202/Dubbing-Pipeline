from __future__ import annotations

import hashlib
import os
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.api.models import Role, User, now_ts
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState, Visibility
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


def test_visibility_no_leaks(tmp_path: Path) -> None:
    _in_dir, out_dir, _logs_dir = _runtime_paths(tmp_path)
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["MIN_FREE_GB"] = "0"
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

        headers_a = login_user(c, username="user_a", password="pass_a", clear_cookies=True)
        headers_b = login_user(c, username="user_b", password="pass_b", clear_cookies=True)

        job_id = "job_visibility_1"
        job_dir = out_dir / "job_visibility_1"
        job_dir.mkdir(parents=True, exist_ok=True)
        output_mkv = job_dir / "job_visibility_1.dub.mp4"
        output_mkv.write_bytes(b"\x00" * 32)
        log_path = job_dir / "job.log"
        log_path.write_text("ok\n", encoding="utf-8")
        stream_dir = job_dir / "stream"
        stream_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = stream_dir / "manifest.json"
        manifest_path.write_text('{"ok": true}', encoding="utf-8")

        job = Job(
            id=job_id,
            owner_id=user_a.id,
            video_path=str(_in_dir / "source.mp4"),
            duration_s=10.0,
            mode="low",
            device="cpu",
            src_lang="ja",
            tgt_lang="en",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            state=JobState.DONE,
            progress=1.0,
            message="done",
            output_mkv=str(output_mkv),
            output_srt="",
            work_dir=str(job_dir),
            log_path=str(log_path),
            series_title="Series A",
            series_slug="series-a",
            season_number=1,
            episode_number=1,
            visibility=Visibility.private,
        )
        store.put(job)

        rel = output_mkv.relative_to(out_dir).as_posix()
        file_url = f"/files/{rel}"
        vid_hash = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:32]
        video_url = f"/video/{vid_hash}"

        # Owner can see private content.
        series_a = c.get("/api/library/series", headers=headers_a)
        assert series_a.status_code == 200
        assert any(it.get("series_slug") == "series-a" for it in series_a.json())
        search_a = c.get("/api/library/search", headers=headers_a, params={"q": "series"})
        assert search_a.status_code == 200
        assert any(it.get("series_slug") == "series-a" for it in search_a.json())
        assert c.get(f"/api/jobs/{job_id}/stream/manifest", headers=headers_a).status_code == 200
        assert c.get(f"/api/jobs/{job_id}/files", headers=headers_a).status_code == 200
        assert c.get(file_url, headers=headers_a).status_code in {200, 206}
        assert c.get(video_url, headers=headers_a).status_code in {200, 206}

        # Non-owner cannot access private content (no leaks on guessed IDs).
        series_b = c.get("/api/library/series", headers=headers_b)
        assert series_b.status_code == 200
        assert not any(it.get("series_slug") == "series-a" for it in series_b.json())
        search_b = c.get("/api/library/search", headers=headers_b, params={"q": "series"})
        assert search_b.status_code == 200
        assert not any(it.get("series_slug") == "series-a" for it in search_b.json())
        assert c.get(f"/api/jobs/{job_id}/stream/manifest", headers=headers_b).status_code in {
            403,
            404,
        }
        assert c.get(f"/api/jobs/{job_id}/files", headers=headers_b).status_code in {403, 404}
        assert c.get(file_url, headers=headers_b).status_code in {403, 404}
        assert c.get(video_url, headers=headers_b).status_code in {403, 404}

        # Share explicitly, then shared visibility becomes accessible.
        share = c.post(
            f"/api/jobs/{job_id}/visibility",
            headers=headers_a,
            json={"visibility": "shared"},
        )
        assert share.status_code == 200

        series_b_shared = c.get("/api/library/series", headers=headers_b)
        assert series_b_shared.status_code == 200
        assert any(it.get("series_slug") == "series-a" for it in series_b_shared.json())
        search_b_shared = c.get("/api/library/search", headers=headers_b, params={"q": "series"})
        assert search_b_shared.status_code == 200
        assert any(it.get("series_slug") == "series-a" for it in search_b_shared.json())
        assert c.get(f"/api/jobs/{job_id}/stream/manifest", headers=headers_b).status_code == 200
        assert c.get(f"/api/jobs/{job_id}/files", headers=headers_b).status_code == 200
        assert c.get(file_url, headers=headers_b).status_code in {200, 206}
        assert c.get(video_url, headers=headers_b).status_code in {200, 206}
