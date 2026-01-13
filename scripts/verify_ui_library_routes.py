from __future__ import annotations

import os
import tempfile

from fastapi.testclient import TestClient


def main() -> int:
    """
    Basic UI library smoke test:
    - boots the app
    - logs in (cookie session)
    - verifies the new /ui/library pages return HTML
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

        from anime_v2.server import app

        with TestClient(app) as c:
            # Unauthed redirects
            r = c.get("/ui/library", follow_redirects=False)
            assert r.status_code in {302, 307}, r.text

            # login (cookie session)
            r = c.post(
                "/auth/login",
                json={"username": "admin", "password": "password123", "session": True},
            )
            assert r.status_code == 200, r.text
            assert c.cookies.get("session"), "missing session cookie"

            # library pages
            r = c.get("/ui/library")
            assert r.status_code == 200, r.text
            assert "Library" in r.text

            r = c.get("/ui/library/my-show")
            assert r.status_code == 200, r.text
            assert "Seasons" in r.text

            r = c.get("/ui/library/my-show/season/1")
            assert r.status_code == 200, r.text
            assert "Episodes" in r.text

            r = c.get("/ui/library/my-show/season/1/episode/2")
            assert r.status_code == 200, r.text
            assert "Playback" in r.text
            assert "Versions" in r.text

    print("verify_ui_library_routes: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

