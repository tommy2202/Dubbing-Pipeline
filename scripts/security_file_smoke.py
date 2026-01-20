from __future__ import annotations

import hashlib
import os
import tempfile
from concurrent.futures import CancelledError as FuturesCancelledError
from pathlib import Path

from fastapi.testclient import TestClient


def _login(c: TestClient, username: str, password: str) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": username, "password": password, "session": True})
    assert r.status_code == 200, r.text
    d = r.json()
    return {"X-CSRF-Token": d["csrf_token"]}


def _sha256_hex(b: bytes) -> str:
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()


def main() -> int:
    # Tighten limits so we can test rejection cheaply.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        out_dir = (root / "Output").resolve()
        log_dir = (root / "logs").resolve()
        in_dir = (root / "Input").resolve()
        uploads_dir = (in_dir / "uploads").resolve()
        state_dir = (out_dir / "_state").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        in_dir.mkdir(parents=True, exist_ok=True)
        uploads_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)

        os.environ["APP_ROOT"] = str(root)
        os.environ["DUBBING_OUTPUT_DIR"] = str(out_dir)
        os.environ["DUBBING_LOG_DIR"] = str(log_dir)
        os.environ["INPUT_DIR"] = str(in_dir)
        os.environ["INPUT_UPLOADS_DIR"] = str(uploads_dir)
        os.environ["DUBBING_STATE_DIR"] = str(state_dir)
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "adminpass"
        os.environ["COOKIE_SECURE"] = "0"
        os.environ["MAX_UPLOAD_MB"] = "1"

        from dubbing_pipeline.config import get_settings
        from dubbing_pipeline.server import app

        get_settings.cache_clear()

        try:
            with TestClient(app) as c:
                hdr = _login(c, "admin", "adminpass")

                # 1) Traversal attempts
                r = c.get("/api/files", params={"dir": "../"}, headers=hdr)
                assert r.status_code in (400, 404), r.text

                r = c.post(
                    "/api/jobs",
                    json={
                        "mode": "low",
                        "video_path": "../auth.db",
                        "series_title": "Test",
                        "season_number": 1,
                        "episode_number": 1,
                    },
                    headers=hdr,
                )
                assert r.status_code == 400, r.text

                # 2) Oversize upload init rejected
                r = c.post(
                    "/api/uploads/init",
                    json={"filename": "x.mp4", "total_bytes": 2 * 1024 * 1024},
                    headers=hdr,
                )
                assert r.status_code in (400, 413), r.text

                # 3) Oversize chunk rejected (defense-in-depth)
                # Use a small (allowed) init size.
                r = c.post(
                    "/api/uploads/init",
                    json={"filename": "x.mp4", "total_bytes": 1024 * 1024},
                    headers=hdr,
                )
                assert r.status_code == 200, r.text
                up = r.json()
                upload_id = up["upload_id"]
                chunk_bytes = int(up["chunk_bytes"])
                payload = b"x" * (chunk_bytes + 2048)
                r = c.post(
                    f"/api/uploads/{upload_id}/chunk",
                    params={"index": 0, "offset": 0},
                    content=payload,
                    headers={**hdr, "X-Chunk-Sha256": _sha256_hex(payload)},
                )
                assert r.status_code == 400, r.text
        except FuturesCancelledError:
            # Some Starlette/AnyIO combinations can raise CancelledError on shutdown.
            # The assertions above already ran; treat shutdown cancellation as clean exit.
            pass

    print("security_file_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

