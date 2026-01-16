from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState
from dubbing_pipeline.server import app


def _runtime_video_path(tmp_path: Path) -> str:
    root = tmp_path.resolve()
    in_dir = root / "Input"
    out_dir = root / "Output"
    logs_dir = root / "logs"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    vp = in_dir / "Test.mp4"
    if not vp.exists():
        vp.write_bytes(b"\x00" * 1024)
    os.environ["APP_ROOT"] = str(root)
    os.environ["INPUT_DIR"] = str(in_dir)
    os.environ["DUBBING_OUTPUT_DIR"] = str(out_dir)
    os.environ["DUBBING_LOG_DIR"] = str(logs_dir)
    return str(vp)


def _login_admin(c: TestClient) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    assert r.status_code == 200
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def test_transcript_get_put_and_persist(tmp_path: Path) -> None:
    video_path = _runtime_video_path(tmp_path)
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    base = tmp_path / "Output" / "Test"
    base.mkdir(parents=True, exist_ok=True)
    (base / "Test.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n\n2\n00:00:01,000 --> 00:00:02,000\n世界\n\n",
        encoding="utf-8",
    )
    (base / "Test.translated.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n\n2\n00:00:01,000 --> 00:00:02,000\nWorld\n\n",
        encoding="utf-8",
    )

    with TestClient(app) as c:
        headers = _login_admin(c)
        store = c.app.state.job_store
        now = "2026-01-01T00:00:00+00:00"
        store.put(
            Job(
                id="j_tr_1",
                owner_id="u1",
                video_path=video_path,
                duration_s=2.0,
                mode="low",
                device="cpu",
                src_lang="ja",
                tgt_lang="en",
                created_at=now,
                updated_at=now,
                state=JobState.DONE,
                progress=1.0,
                message="Done",
                output_mkv=str(base / "Test.dub.mkv"),
                output_srt=str(base / "Test.translated.srt"),
                work_dir=str(base),
                log_path=str(base / "job.log"),
            )
        )

        g1 = c.get("/api/jobs/j_tr_1/transcript?page=1&per_page=50", headers=headers)
        assert g1.status_code == 200
        data = g1.json()
        assert data["total"] == 2
        assert data["items"][0]["tgt_text"] == "Hello"

        p = c.put(
            "/api/jobs/j_tr_1/transcript",
            headers=headers,
            json={
                "updates": [
                    {"index": 1, "tgt_text": "Hello there", "approved": True, "flags": ["approved"]}
                ]
            },
        )
        assert p.status_code == 200

        g2 = c.get("/api/jobs/j_tr_1/transcript?page=1&per_page=50", headers=headers)
        assert g2.status_code == 200
        data2 = g2.json()
        assert data2["items"][0]["tgt_text"] == "Hello there"
        assert data2["items"][0]["approved"] is True


def test_transcript_synthesize_sets_resynth_flag(tmp_path: Path) -> None:
    video_path = _runtime_video_path(tmp_path)
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    base = tmp_path / "Output" / "Test"
    base.mkdir(parents=True, exist_ok=True)
    (base / "Test.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n\n", encoding="utf-8")

    with TestClient(app) as c:
        headers = _login_admin(c)
        store = c.app.state.job_store
        now = "2026-01-01T00:00:00+00:00"
        store.put(
            Job(
                id="j_tr_2",
                owner_id="u1",
                video_path=video_path,
                duration_s=1.0,
                mode="low",
                device="cpu",
                src_lang="ja",
                tgt_lang="en",
                created_at=now,
                updated_at=now,
                state=JobState.DONE,
                progress=1.0,
                message="Done",
                output_mkv=str(base / "Test.dub.mkv"),
                output_srt=str(base / "Test.translated.srt"),
                work_dir=str(base),
                log_path=str(base / "job.log"),
            )
        )
        r = c.post("/api/jobs/j_tr_2/transcript/synthesize", headers=headers)
        assert r.status_code == 200
        j = store.get("j_tr_2")
        assert j is not None
        assert j.state == JobState.QUEUED
        assert isinstance(j.runtime.get("resynth"), dict)
