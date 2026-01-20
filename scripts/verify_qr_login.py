from __future__ import annotations

import os
import tempfile
from concurrent.futures import CancelledError as FuturesCancelledError
from pathlib import Path

from fastapi.testclient import TestClient


def main() -> int:
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
        os.environ["ENABLE_QR_LOGIN"] = "1"

        from dubbing_pipeline.config import get_settings
        from dubbing_pipeline.server import app

        get_settings.cache_clear()

        try:
            with TestClient(app) as c_admin:
                r = c_admin.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "adminpass", "session": True},
                )
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
        except FuturesCancelledError:
            # Some Starlette/AnyIO combinations can raise CancelledError on shutdown.
            # The assertions above already ran; treat shutdown cancellation as clean exit.
            pass

    print("verify_qr_login: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

