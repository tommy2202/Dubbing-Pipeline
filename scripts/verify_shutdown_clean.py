from __future__ import annotations

import os
import sys
import tempfile

from fastapi.testclient import TestClient


def main() -> int:
    """
    Shutdown safety smoke test:
    - boot app via TestClient
    - login and hit library + dashboard endpoints
    - exit cleanly without shutdown exceptions
    """
    with tempfile.TemporaryDirectory() as td:
        root = td
        out_dir = os.path.join(root, "Output")
        os.makedirs(out_dir, exist_ok=True)

        os.environ.setdefault("APP_ROOT", root)
        os.environ.setdefault("OUTPUT_DIR", out_dir)
        os.environ.setdefault("REMOTE_ACCESS_MODE", "off")
        os.environ.setdefault("COOKIE_SECURE", "0")
        os.environ.setdefault("ADMIN_USERNAME", "admin")
        os.environ.setdefault("ADMIN_PASSWORD", "password123")

        # Clear cached settings (important when running multiple verifies in same interpreter).
        try:
            from config import settings as cfg_settings

            cfg_settings.get_settings.cache_clear()
        except Exception:
            pass

        from dubbing_pipeline.server import app

        try:
            with TestClient(app) as c:
                r = c.get("/ui/login")
                assert r.status_code == 200, r.text

                r = c.post(
                    "/auth/login",
                    json={"username": "admin", "password": "password123", "session": True},
                )
                assert r.status_code == 200, r.text
                assert c.cookies.get("session"), "missing session cookie"
                assert c.cookies.get("csrf"), "missing csrf cookie"

                r = c.get("/ui/library")
                assert r.status_code == 200, r.text

                r = c.get("/ui/dashboard")
                assert r.status_code == 200, r.text
        except Exception as ex:
            print(f"verify_shutdown_clean: FAIL ({ex})", file=sys.stderr)
            raise

    print("verify_shutdown_clean: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
