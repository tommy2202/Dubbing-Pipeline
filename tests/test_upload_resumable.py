from __future__ import annotations

import hashlib
import os
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.server import app
from tests._helpers.auth import login_admin


def _setup_env(tmp_path: Path) -> None:
    os.environ["APP_ROOT"] = str(tmp_path)
    os.environ["DUBBING_OUTPUT_DIR"] = str(tmp_path / "Output")
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["UPLOAD_CHUNK_BYTES"] = "4"
    os.environ["MAX_UPLOAD_BYTES"] = "1048576"
    get_settings.cache_clear()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_upload_resumable(tmp_path: Path, monkeypatch) -> None:
    _setup_env(tmp_path)
    import dubbing_pipeline.web.routes.uploads as uploads_mod

    monkeypatch.setattr(uploads_mod, "_validate_media_or_400", lambda *_args, **_kwargs: 1.0)

    payload = b"hello resumable upload"
    final_sha = _sha256(payload)

    with TestClient(app) as c:
        headers = login_admin(c)
        init = c.post(
            "/api/uploads/init",
            json={"filename": "clip.mp4", "total_bytes": len(payload), "mime": "video/mp4"},
            headers=headers,
        )
        assert init.status_code == 200, init.text
        init_data = init.json()
        upload_id = init_data["upload_id"]
        chunk_bytes = int(init_data["chunk_bytes"])
        total_chunks = int(init_data["total_chunks"])
        assert total_chunks >= 2

        first = payload[:chunk_bytes]
        r0 = c.post(
            f"/api/uploads/{upload_id}/chunk?index=0&offset=0",
            data=first,
            headers={**headers, "X-Chunk-Sha256": _sha256(first)},
        )
        assert r0.status_code == 200, r0.text

        resume = c.get(f"/api/uploads/{upload_id}/resume", headers=headers)
        assert resume.status_code == 200, resume.text
        missing = resume.json().get("missing_chunks", [])
        assert 0 not in set(missing)

        for idx in range(1, total_chunks):
            start = idx * chunk_bytes
            chunk = payload[start : start + chunk_bytes]
            r = c.post(
                f"/api/uploads/{upload_id}/chunk?index={idx}&offset={start}",
                data=chunk,
                headers={**headers, "X-Chunk-Sha256": _sha256(chunk)},
            )
            assert r.status_code == 200, r.text

        done = c.post(
            f"/api/uploads/{upload_id}/complete",
            json={"final_sha256": final_sha},
            headers=headers,
        )
        assert done.status_code == 200, done.text
        video_path = Path(done.json()["video_path"])
        assert video_path.exists()
        assert _sha256(video_path.read_bytes()) == final_sha
