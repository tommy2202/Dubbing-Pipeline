from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.server import app


def _login_admin(c: TestClient) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
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
        os.environ["MAX_UPLOAD_BYTES"] = "1024"
        os.environ["UPLOAD_CHUNK_BYTES"] = "4"

        get_settings.cache_clear()

        with TestClient(app) as c:
            headers = _login_admin(c)

            r = c.post(
                "/api/uploads/init",
                json={"filename": "../evil.mp4", "total_bytes": 8, "mime": "video/mp4"},
                headers=headers,
            )
            assert r.status_code == 400

            r2 = c.post(
                "/api/uploads/init",
                json={"filename": "big.mp4", "total_bytes": 4096, "mime": "video/mp4"},
                headers=headers,
            )
            assert r2.status_code == 413

            ok = c.post(
                "/api/uploads/init",
                json={"filename": "clip.mp4", "total_bytes": 8, "mime": "video/mp4"},
                headers=headers,
            )
            assert ok.status_code == 200, ok.text
            upload_id = ok.json()["upload_id"]

            body = b"abcd"
            sha = hashlib.sha256(body).hexdigest()
            r3 = c.post(
                f"/api/uploads/{upload_id}/chunk?index=1&offset=4",
                data=body,
                headers={**headers, "X-Chunk-Sha256": sha},
            )
            assert r3.status_code == 409

        print("verify_upload_hardening: OK")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
