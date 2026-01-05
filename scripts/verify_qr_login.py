from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient


def main() -> int:
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(Path("/tmp") / "anime_v2_qr_out")
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["ENABLE_QR_LOGIN"] = "1"

    from anime_v2.config import get_settings
    from anime_v2.server import app

    get_settings.cache_clear()

    with TestClient(app) as c_admin:
        r = c_admin.post("/api/auth/login", json={"username": "admin", "password": "adminpass", "session": True})
        assert r.status_code == 200
        csrf = r.json().get("csrf_token") or ""
        assert csrf

        r2 = c_admin.post("/api/auth/qr/init", json={}, headers={"X-CSRF-Token": csrf})
        assert r2.status_code == 200
        code = r2.json().get("code") or ""
        assert code.startswith("qr_")

        # Redeem with a fresh client (simulates phone)
        with TestClient(app) as c_phone:
            r3 = c_phone.post("/api/auth/qr/redeem", json={"code": code})
            assert r3.status_code == 200
            # Cookie session should now work on authenticated endpoints
            r4 = c_phone.get("/api/settings")
            assert r4.status_code == 200

            # Single-use: redeem again must fail
            r5 = c_phone.post("/api/auth/qr/redeem", json={"code": code})
            assert r5.status_code in (401, 400)

    print("verify_qr_login: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

