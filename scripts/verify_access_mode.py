from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient


def _login_admin(c: TestClient) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    assert r.status_code == 200, r.text
    data = r.json()
    headers = {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}
    c.cookies.clear()
    return headers


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
        os.environ["ACCESS_MODE"] = "tunnel"
        os.environ["TRUST_PROXY_HEADERS"] = "1"
        os.environ["TRUSTED_PROXIES"] = ""
        os.environ["PUBLIC_BASE_URL"] = "https://example.local"

        from dubbing_pipeline.config import get_settings
        from dubbing_pipeline.server import app

        get_settings.cache_clear()

        with TestClient(app) as c:
            headers = _login_admin(c)
            r = c.get("/api/system/security-posture", headers=headers)
            assert r.status_code == 200, r.text
            data = r.json()
            assert data.get("access_mode") == "cloudflare"
            assert data.get("access_mode_raw") == "tunnel"
            assert data.get("public_base_url") == "https://example.local"
            assert data.get("effective_trust_proxy_headers") is False
            warnings = data.get("warnings") or []
            assert any("TRUSTED_PROXY_SUBNETS" in str(w) for w in warnings)

        print("verify_access_mode: ok")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
