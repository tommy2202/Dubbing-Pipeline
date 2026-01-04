from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from anime_v2.api.deps import require_scope
from anime_v2.api.models import AuthStore, Role, User, now_ts
from anime_v2.api.routes_auth import router as auth_router
from anime_v2.api.security import issue_csrf_token
from anime_v2.utils.crypto import PasswordHasher, random_id
from anime_v2.utils.ratelimit import RateLimiter


def _make_app(tmp: Path) -> FastAPI:
    app = FastAPI()

    @app.on_event("startup")
    async def _startup():
        app.state.auth_store = AuthStore(tmp / "auth.db")
        app.state.rate_limiter = RateLimiter()

        # Create a test user
        ph = PasswordHasher()
        u = User(
            id="u_test",
            username="alice",
            password_hash=ph.hash("password123"),
            role=Role.operator,
            totp_secret=None,
            totp_enabled=False,
            created_at=now_ts(),
        )
        app.state.auth_store.upsert_user(u)

    app.include_router(auth_router)

    @app.get("/protected")
    async def protected(_=Depends(require_scope("read:job"))):
        return {"ok": True}

    @app.post("/protected")
    async def protected_post(request: Request, _=Depends(require_scope("submit:job"))):
        # return something deterministic
        return {"ok": True, "csrf": request.headers.get("x-csrf-token") or ""}

    return app


def main() -> int:
    # Ensure legacy token-in-url is off in tests
    os.environ["ALLOW_LEGACY_TOKEN_LOGIN"] = "0"

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        app = _make_app(tmp)
        # Ensure startup events run (auth_store, limiter, user bootstrap).
        with TestClient(app) as c:

            # 1) login fail
            r = c.post("/auth/login", json={"username": "alice", "password": "wrong", "session": True})
            assert r.status_code == 401, r.text

            # 2) rate limit (5/min) for login
            for _i in range(5):
                _ = c.post("/auth/login", json={"username": "alice", "password": "wrong", "session": True})
            r2 = c.post("/auth/login", json={"username": "alice", "password": "wrong", "session": True})
            assert r2.status_code == 429, f"expected 429 rate limit, got {r2.status_code}"

            # Reset limiter state for the next phase (login success)
            app.state.rate_limiter = RateLimiter()

            # 3) login success (session cookie)
            r = c.post("/auth/login", json={"username": "alice", "password": "password123", "session": True})
            assert r.status_code == 200, r.text
            data = r.json()
            assert "access_token" in data and data["access_token"], "missing access_token"
            assert c.cookies.get("refresh"), "missing refresh cookie"
            assert c.cookies.get("csrf"), "missing csrf cookie"
            assert c.cookies.get("session"), "missing session cookie"

            # 4) GET should work with cookie session (no CSRF header required)
            r = c.get("/protected")
            assert r.status_code == 200, r.text

            # 5) POST should require CSRF header
            r = c.post("/protected", json={"x": 1})
            assert r.status_code == 403, f"expected CSRF 403, got {r.status_code}"

            csrf = c.cookies.get("csrf") or ""
            r = c.post("/protected", json={"x": 1}, headers={"X-CSRF-Token": csrf})
            assert r.status_code == 200, r.text

            # 6) refresh rotation works; old refresh should become invalid
            old_refresh = c.cookies.get("refresh") or ""
            assert old_refresh
            # refresh requires CSRF too
            r = c.post("/auth/refresh", headers={"X-CSRF-Token": csrf})
            assert r.status_code == 200, r.text
            new_refresh = c.cookies.get("refresh") or ""
            assert new_refresh and new_refresh != old_refresh, "refresh did not rotate"

            # Attempt to reuse old refresh (replay) should fail
            with TestClient(app) as c2:
                c2.cookies.set("refresh", old_refresh)
                c2.cookies.set("csrf", issue_csrf_token())
                r = c2.post("/auth/refresh", headers={"X-CSRF-Token": c2.cookies.get("csrf")})
                assert r.status_code == 401, f"expected 401 replay, got {r.status_code}"

        print("verify_auth_flow: OK")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

