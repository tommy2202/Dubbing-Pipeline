from __future__ import annotations

import os
import tempfile

from fastapi.testclient import TestClient


def main() -> int:
    """
    Basic UI smoke test:
    - boots the app
    - logs in (cookie session)
    - checks core UI routes return HTML
    - asserts new mobile tabs exist on job detail page
    - ensures new endpoints are registered in OpenAPI
    """
    with tempfile.TemporaryDirectory() as td:
        root = td
        out_dir = os.path.join(root, "Output")
        os.makedirs(out_dir, exist_ok=True)

        # Configure environment BEFORE importing the app/settings.
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

        with TestClient(app) as c:
            # login page
            r = c.get("/ui/login")
            assert r.status_code == 200, r.text

            # login (cookie session)
            r = c.post("/auth/login", json={"username": "admin", "password": "password123", "session": True})
            assert r.status_code == 200, r.text
            assert c.cookies.get("session"), "missing session cookie"
            assert c.cookies.get("csrf"), "missing csrf cookie"

            # dashboard + jobs list
            r = c.get("/ui/dashboard")
            assert r.status_code == 200, r.text
            r = c.get("/ui/partials/jobs_table?limit=1")
            assert r.status_code == 200, r.text

            # job detail page renders even for unknown job id (UI is client-driven)
            fake = "job_test_123"
            r = c.get(f"/ui/jobs/{fake}")
            assert r.status_code == 200, r.text
            body = r.text
            assert "Playback" in body, "missing Playback tab label"
            assert "Progress/Logs" in body, "missing Progress/Logs tab label"
            assert "Review/Edit" in body, "missing Review/Edit tab label"
            assert "Overrides" in body, "missing Overrides tab label"

            # OpenAPI includes the new endpoints (route availability)
            r = c.get("/openapi.json")
            assert r.status_code == 200, r.text
            spec = r.json()
            paths = spec.get("paths") if isinstance(spec, dict) else {}
            assert "/api/jobs/{id}/overrides/music/effective" in paths, "missing music effective endpoint"
            assert (
                "/api/jobs/{id}/review/segments/{segment_id}/helper" in paths
            ), "missing review helper endpoint"

    print("verify_ui_routes: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

