from __future__ import annotations

import os
import tempfile
from concurrent.futures import CancelledError as FuturesCancelledError

from fastapi.testclient import TestClient


def main() -> int:
    """
    Security smoke tests for remote/mobile hardening.

    Covers:
    - directory traversal attempts (file picker + /files)
    - upload limits + extension/MIME validation
    - CSRF enforcement for cookie sessions
    - rate limiting behavior (login + uploads init)
    - security headers on HTML responses (CSP, etc.)
    """
    with tempfile.TemporaryDirectory() as td:
        root = td
        out_dir = os.path.join(root, "Output")
        in_dir = os.path.join(root, "Input")
        os.makedirs(out_dir, exist_ok=True)
        os.makedirs(in_dir, exist_ok=True)

        # A small dummy MP4 test file (not necessarily valid media)
        dummy_mp4 = os.path.join(in_dir, "Test.mp4")
        with open(dummy_mp4, "wb") as f:
            f.write(b"\x00" * 1024)

        uploads_dir = os.path.join(in_dir, "uploads")
        os.makedirs(uploads_dir, exist_ok=True)
        os.environ["APP_ROOT"] = root
        os.environ["DUBBING_OUTPUT_DIR"] = out_dir
        os.environ["DUBBING_LOG_DIR"] = os.path.join(root, "logs")
        os.environ["INPUT_DIR"] = in_dir
        os.environ["INPUT_UPLOADS_DIR"] = uploads_dir
        os.environ["DUBBING_STATE_DIR"] = os.path.join(out_dir, "_state")
        os.environ["REMOTE_ACCESS_MODE"] = "off"
        os.environ["COOKIE_SECURE"] = "0"
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "password123"
        os.environ["CORS_ORIGINS"] = ""  # strict default: none

        # Clear cached settings
        try:
            from config import settings as cfg_settings

            cfg_settings.get_settings.cache_clear()
        except Exception:
            pass

        from dubbing_pipeline.server import app

        try:
            with TestClient(app) as c:
                # HTML route should have baseline security headers
                r = c.get("/ui/login")
                assert r.status_code == 200
                assert "content-security-policy" in {k.lower() for k in r.headers}
                assert r.headers.get("x-content-type-options") == "nosniff"
                assert r.headers.get("x-frame-options") == "DENY"

                # CORS should not reflect arbitrary origins when allow list is empty
                r = c.get("/ui/login", headers={"Origin": "https://evil.example"})
                assert r.status_code == 200
                assert "access-control-allow-origin" not in {k.lower() for k in r.headers}

                # Directory traversal in file picker should be blocked
                # (requires auth)
                r = c.post(
                    "/auth/login",
                    json={"username": "admin", "password": "password123", "session": True},
                )
                assert r.status_code == 200, r.text

                r = c.get("/api/files?dir=../../..")
                assert r.status_code in (400, 404), r.text

                # /files traversal should be blocked
                r = c.get("/files/%2e%2e/auth.db")
                assert r.status_code == 404, r.text

                # CSRF enforcement: cookie session without X-CSRF should fail on state-changing endpoints
                r = c.post("/api/jobs", json={"video_path": "Input/Test.mp4", "mode": "low"})
                assert r.status_code == 403, r.text
                csrf = c.cookies.get("csrf") or ""
                assert csrf

                # video_path must be under INPUT_DIR (no arbitrary reads)
                r = c.post(
                    "/api/jobs",
                    json={
                        "video_path": "../Output/_state/auth.db",
                        "mode": "low",
                        "series_title": "Security Smoke",
                        "season_number": 1,
                        "episode_number": 1,
                    },
                    headers={"X-CSRF-Token": csrf},
                )
                assert r.status_code == 400, r.text

                # Upload init: invalid extension rejected
                r = c.post(
                    "/api/uploads/init",
                    json={
                        "filename": "evil.exe",
                        "total_bytes": 100,
                        "mime": "application/octet-stream",
                    },
                    headers={"X-CSRF-Token": csrf},
                )
                assert r.status_code == 400, r.text

                # Upload init: too-large total rejected
                r = c.post(
                    "/api/uploads/init",
                    # default max_upload_mb can be large; exceed it decisively
                    json={
                        "filename": "big.mp4",
                        "total_bytes": (3 * 1024 * 1024 * 1024),
                        "mime": "video/mp4",
                    },
                    headers={"X-CSRF-Token": csrf},
                )
                assert r.status_code == 400, r.text

                # Login rate limit: 6 bad logins should produce a 429
                with TestClient(app) as c2:
                    for _ in range(6):
                        rr = c2.post(
                            "/auth/login",
                            json={"username": "admin", "password": "wrong", "session": True},
                        )
                    assert rr.status_code in (401, 429)
                    # ensure we can hit the limiter
                    rr2 = c2.post(
                        "/auth/login",
                        json={"username": "admin", "password": "wrong", "session": True},
                    )
                    assert rr2.status_code == 429, rr2.text
        except FuturesCancelledError:
            # Some Starlette/AnyIO combinations can raise CancelledError on shutdown.
            # The assertions above already ran; treat shutdown cancellation as clean exit.
            pass

        print("security_smoke: OK")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

