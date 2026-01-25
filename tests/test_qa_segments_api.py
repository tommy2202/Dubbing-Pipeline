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


def _seed_job(tmp_path: Path) -> tuple[str, Path]:
    video_path = _runtime_video_path(tmp_path)
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
    return video_path, base


def test_segments_edit_and_approve(tmp_path: Path) -> None:
    video_path, base = _seed_job(tmp_path)
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
                id="j_seg_1",
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

        g1 = c.get("/api/jobs/j_seg_1/segments", headers=headers)
        assert g1.status_code == 200
        data = g1.json()
        assert data["total"] == 2
        assert data["items"][0]["chosen_text"] == "Hello"

        p = c.patch(
            "/api/jobs/j_seg_1/segments/1",
            headers=headers,
            json={"translated_text": "Hello there", "pronunciation_overrides": {"term": "test"}},
        )
        assert p.status_code == 200

        st_path = base / "transcript_store.json"
        assert st_path.exists()
        store_json = st_path.read_text(encoding="utf-8")
        assert "Hello there" in store_json

        qa = store.get_qa_review(job_id="j_seg_1", segment_id=1)
        assert qa is not None
        assert qa["status"] == "pending"

        a = c.post("/api/jobs/j_seg_1/segments/1/approve", headers=headers)
        assert a.status_code == 200
        qa2 = store.get_qa_review(job_id="j_seg_1", segment_id=1)
        assert qa2 is not None
        assert qa2["status"] == "approved"


def test_segments_rerun_enqueues(tmp_path: Path) -> None:
    video_path, base = _seed_job(tmp_path)
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    get_settings.cache_clear()

    class StubQueue:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []

        async def submit_job(
            self,
            *,
            job_id: str,
            user_id: str,
            mode: str,
            device: str,
            priority: int = 100,
            meta: dict | None = None,
        ) -> None:
            self.calls.append(
                {
                    "job_id": str(job_id),
                    "user_id": str(user_id),
                    "mode": str(mode),
                    "device": str(device),
                }
            )

    with TestClient(app) as c:
        headers = _login_admin(c)
        store = c.app.state.job_store
        c.app.state.queue_backend = StubQueue()
        now = "2026-01-01T00:00:00+00:00"
        store.put(
            Job(
                id="j_seg_2",
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

        r = c.post(
            "/api/jobs/j_seg_2/segments/rerun",
            headers=headers,
            json={"segment_ids": [1]},
        )
        assert r.status_code == 200
        qb = c.app.state.queue_backend
        assert len(qb.calls) == 1
        job = store.get("j_seg_2")
        assert job is not None
        assert isinstance(job.runtime.get("resynth"), dict)
