#!/usr/bin/env python3
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _login(c: TestClient, username: str, password: str) -> str:
    r = c.post("/auth/login", json={"username": username, "password": password, "session": True})
    assert r.status_code == 200, r.text
    token = r.json().get("csrf_token") or ""
    assert token
    return str(token)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        out = (root / "Output").resolve()
        out.mkdir(parents=True, exist_ok=True)

        os.environ["APP_ROOT"] = str(root)
        os.environ["DUBBING_OUTPUT_DIR"] = str(out)
        os.environ["DUBBING_LOG_DIR"] = str(out / "logs")
        os.environ["COOKIE_SECURE"] = "0"
        os.environ["STRICT_SECRETS"] = "0"

        from dubbing_pipeline.api.models import AuthStore, Role, User, now_ts
        from dubbing_pipeline.api.routes_auth import router as auth_router
        from dubbing_pipeline.api.routes_system import router as system_router
        from dubbing_pipeline.utils.crypto import PasswordHasher

        app = FastAPI()
        state_root = (out / "_state").resolve()
        state_root.mkdir(parents=True, exist_ok=True)
        auth_store = AuthStore(state_root / "auth.db")
        app.state.auth_store = auth_store

        ph = PasswordHasher()
        auth_store.upsert_user(
            User(
                id="u_admin",
                username="admin",
                password_hash=ph.hash("adminpass"),
                role=Role.admin,
                totp_secret=None,
                totp_enabled=False,
                created_at=now_ts(),
            )
        )

        app.include_router(auth_router)
        app.include_router(system_router)

        with TestClient(app) as c:
            _login(c, "admin", "adminpass")
            r = c.get("/api/system/readiness")
            assert r.status_code == 200, r.text
            data = r.json()
            items = data.get("items") if isinstance(data, dict) else None
            assert isinstance(items, list) and items, items
            keys = {str(it.get("key")) for it in items if isinstance(it, dict)}
            expected = {
                "gpu_cuda",
                "whisper_models",
                "translation_whisper",
                "translation_offline",
                "xtts",
                "diarization",
                "separation",
                "lipsync",
                "storage_backend",
                "retention",
            }
            missing = expected - keys
            assert not missing, f"missing keys: {sorted(missing)}"
            for it in items:
                status = str(it.get("status") or "")
                assert status in {"OK", "Missing", "Disabled"}, status

    print("verify_readiness: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
