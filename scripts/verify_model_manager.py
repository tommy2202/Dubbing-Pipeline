from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient


def _login(c: TestClient, username: str, password: str) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": username, "password": password, "session": True})
    assert r.status_code == 200, r.text
    d = r.json()
    return {"X-CSRF-Token": d["csrf_token"]}


def main() -> int:
    out = Path("/tmp/anime_v2_verify_models").resolve()
    out.mkdir(parents=True, exist_ok=True)
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(out)
    os.environ["ANIME_V2_LOG_DIR"] = str(out / "logs")
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["ENABLE_MODEL_DOWNLOADS"] = "0"

    from anime_v2.config import get_settings
    from anime_v2.server import app

    get_settings.cache_clear()

    with TestClient(app) as c:
        hdr = _login(c, "admin", "adminpass")
        r = c.get("/api/runtime/models", headers=hdr)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("ok") is True
        assert "paths" in data and "disk" in data

        # prewarm should be blocked unless enabled
        r2 = c.post("/api/runtime/models/prewarm?preset=low", headers=hdr)
        assert r2.status_code == 400

    print("verify_model_manager: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

