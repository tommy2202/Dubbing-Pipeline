from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient


def main() -> int:
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(Path("/tmp") / "anime_v2_sessions_out")
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"

    from anime_v2.config import get_settings
    from anime_v2.server import app

    get_settings.cache_clear()

    with TestClient(app) as c1:
        r1 = c1.post("/api/auth/login", json={"username": "admin", "password": "adminpass", "session": True})
        assert r1.status_code == 200
        csrf1 = r1.json().get("csrf_token") or ""
        assert csrf1

        with TestClient(app) as c2:
            r2 = c2.post("/api/auth/login", json={"username": "admin", "password": "adminpass", "session": True})
            assert r2.status_code == 200

            # list from c1 (should see >=1 session)
            s1 = c1.get("/api/auth/sessions")
            assert s1.status_code == 200
            items = s1.json().get("items") or []
            assert isinstance(items, list)
            assert len(items) >= 1
            dev0 = items[0].get("device_id") or ""
            assert dev0

            # revoke one
            rr = c1.post(f"/api/auth/sessions/{dev0}/revoke", headers={"X-CSRF-Token": csrf1})
            assert rr.status_code == 200

            # revoke all
            ra = c1.post("/api/auth/sessions/revoke_all", headers={"X-CSRF-Token": csrf1})
            assert ra.status_code == 200

    print("verify_sessions: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

