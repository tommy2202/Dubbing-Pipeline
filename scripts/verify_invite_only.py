from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient


def _login_admin(c: TestClient) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    assert r.status_code == 200
    d = r.json()
    return {"Authorization": f"Bearer {d['access_token']}", "X-CSRF-Token": d["csrf_token"]}


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        out_root = (root / "Output").resolve()
        in_root = (root / "Input").resolve()
        logs_root = (root / "logs").resolve()
        out_root.mkdir(parents=True, exist_ok=True)
        in_root.mkdir(parents=True, exist_ok=True)
        logs_root.mkdir(parents=True, exist_ok=True)

        os.environ["APP_ROOT"] = str(root)
        os.environ["INPUT_DIR"] = str(in_root)
        os.environ["DUBBING_OUTPUT_DIR"] = str(out_root)
        os.environ["DUBBING_LOG_DIR"] = str(logs_root)
        os.environ["DUBBING_STATE_DIR"] = str(root / "_state")
        os.environ["MIN_FREE_GB"] = "0"
        os.environ["DUBBING_SKIP_STARTUP_CHECK"] = "1"
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "adminpass"
        os.environ["COOKIE_SECURE"] = "0"

        from dubbing_pipeline.config import get_settings
        from dubbing_pipeline.server import app

        get_settings.cache_clear()

        with TestClient(app) as c:
            headers = _login_admin(c)

            reg = c.post("/api/auth/register", json={"username": "self", "password": "password123"})
            assert reg.status_code in {403, 404}

            inv = c.post("/api/admin/invites", headers=headers, json={"expires_in_hours": 1})
            assert inv.status_code == 200
            invite_url = str(inv.json().get("invite_url") or "")
            assert invite_url
            token = invite_url.rsplit("/", 1)[-1]

            ui = c.get(f"/invite/{token}")
            assert ui.status_code == 200

            redeem = c.post(
                "/api/invites/redeem",
                json={"token": token, "username": "invited_user", "password": "passw0rd123"},
            )
            assert redeem.status_code == 200

            login = c.post(
                "/api/auth/login", json={"username": "invited_user", "password": "passw0rd123"}
            )
            assert login.status_code == 200

            reuse = c.post(
                "/api/invites/redeem",
                json={"token": token, "username": "invited_user2", "password": "passw0rd123"},
            )
            assert reuse.status_code in {400, 410}

        print("verify_invite_only: ok")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
