from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.server import app
from dubbing_pipeline.utils.ratelimit import RateLimiter


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
        get_settings.cache_clear()

        with TestClient(app) as c:
            c.app.state.rate_limiter = RateLimiter()

            statuses = []
            for _ in range(6):
                r = c.post("/api/auth/login", json={"username": "admin", "password": "bad"})
                statuses.append(r.status_code)
            assert 429 in statuses

            login = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
            assert login.status_code == 200, login.text
            data = login.json()
            headers = {
                "Authorization": f"Bearer {data['access_token']}",
                "X-CSRF-Token": data["csrf_token"],
            }

            init = c.post(
                "/api/uploads/init",
                json={"filename": "clip.mp4", "total_bytes": 4, "mime": "video/mp4"},
                headers=headers,
            )
            assert init.status_code == 200, init.text
            upload_id = init.json()["upload_id"]

            body = b"test"
            sha = hashlib.sha256(body).hexdigest()
            rl = c.app.state.rate_limiter
            counts: dict[str, int] = {}
            original = rl.allow

            def _allow(key: str, *, limit: int, per_seconds: int) -> bool:
                if key.startswith("upload:chunk"):
                    counts[key] = counts.get(key, 0) + 1
                    return counts[key] <= 2
                return original(key, limit=limit, per_seconds=per_seconds)

            with patch.object(rl, "allow", side_effect=_allow):
                r1 = c.post(
                    f"/api/uploads/{upload_id}/chunk?index=0&offset=0",
                    data=body,
                    headers={**headers, "X-Chunk-Sha256": sha},
                )
                r2 = c.post(
                    f"/api/uploads/{upload_id}/chunk?index=0&offset=0",
                    data=body,
                    headers={**headers, "X-Chunk-Sha256": sha},
                )
                r3 = c.post(
                    f"/api/uploads/{upload_id}/chunk?index=0&offset=0",
                    data=body,
                    headers={**headers, "X-Chunk-Sha256": sha},
                )
            assert r1.status_code == 200
            assert r2.status_code == 200
            assert r3.status_code == 429

        print("verify_rate_limits: OK")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
