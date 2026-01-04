from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from anime_v2.config import get_settings
from anime_v2.jobs.models import Job, JobState
from anime_v2.server import app


def _login_admin(c: TestClient) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    assert r.status_code == 200
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def test_job_characters_persist(tmp_path: Path) -> None:
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    with TestClient(app) as c:
        headers = _login_admin(c)
        store = c.app.state.job_store
        now = "2026-01-01T00:00:00+00:00"
        store.put(
            Job(
                id="j_char_1",
                owner_id="u1",
                video_path="/workspace/Input/Test.mp4",
                duration_s=10.0,
                mode="low",
                device="cpu",
                src_lang="ja",
                tgt_lang="en",
                created_at=now,
                updated_at=now,
                state=JobState.QUEUED,
                progress=0.0,
                message="Queued",
                output_mkv="",
                output_srt="",
                work_dir=str(tmp_path / "Output" / "Test"),
                log_path=str(tmp_path / "Output" / "Test" / "job.log"),
            )
        )
        put = c.put(
            "/api/jobs/j_char_1/characters",
            headers=headers,
            json={
                "items": [
                    {
                        "character_id": "SPEAKER_01",
                        "label": "Alice",
                        "speaker_strategy": "preset",
                        "tts_speaker": "default",
                        "language": "en",
                    }
                ]
            },
        )
        assert put.status_code == 200
        get = c.get("/api/jobs/j_char_1/characters", headers=headers)
        assert get.status_code == 200
        data = get.json()
        assert isinstance(data.get("items"), list)
        assert data["items"][0]["character_id"] == "SPEAKER_01"


def test_logs_stream_endpoint_returns_sse(tmp_path: Path) -> None:
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    log_dir = tmp_path / "Output" / "Test"
    log_dir.mkdir(parents=True, exist_ok=True)
    lp = log_dir / "job.log"
    lp.write_text("hello\n", encoding="utf-8")

    with TestClient(app) as c:
        headers = _login_admin(c)
        store = c.app.state.job_store
        now = "2026-01-01T00:00:00+00:00"
        store.put(
            Job(
                id="j_log_1",
                owner_id="u1",
                video_path="/workspace/Input/Test.mp4",
                duration_s=10.0,
                mode="low",
                device="cpu",
                src_lang="ja",
                tgt_lang="en",
                created_at=now,
                updated_at=now,
                state=JobState.RUNNING,
                progress=0.1,
                message="Running",
                output_mkv="",
                output_srt="",
                work_dir=str(log_dir),
                log_path=str(lp),
            )
        )
        r = c.get("/api/jobs/j_log_1/logs/stream?once=1", headers=headers)
        assert r.status_code == 200
        assert "text/event-stream" in (r.headers.get("content-type") or "")
        assert "hello" in r.text
