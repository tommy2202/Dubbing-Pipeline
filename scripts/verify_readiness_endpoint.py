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
    out = Path("/tmp/dubbing_pipeline_verify_readiness").resolve()
    out.mkdir(parents=True, exist_ok=True)
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["DUBBING_OUTPUT_DIR"] = str(out)
    os.environ["DUBBING_LOG_DIR"] = str(out / "logs")
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"

    from dubbing_pipeline.config import get_settings
    from dubbing_pipeline.server import app

    get_settings.cache_clear()

    with TestClient(app) as c:
        hdr = _login(c, "admin", "adminpass")
        r = c.get("/api/system/readiness", headers=hdr)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("ok") is True
        sections = data.get("sections")
        assert isinstance(sections, list) and sections, "missing sections"
        first = sections[0]
        assert isinstance(first, dict) and "title" in first and "items" in first
        for sec in sections:
            items = sec.get("items", [])
            assert isinstance(items, list), "items must be list"
            for item in items:
                assert "status" in item and "reason" in item and "action" in item

    print("verify_readiness_endpoint: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
