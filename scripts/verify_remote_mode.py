from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import FastAPI

from anime_v2.api.remote_access import remote_access_middleware
from config.settings import get_settings


def _reset_settings_env(env: dict[str, str]) -> None:
    for k in list(env.keys()):
        os.environ.pop(k, None)
    os.environ.update(env)
    # settings is an lru_cache; clear between scenarios
    get_settings.cache_clear()  # type: ignore[attr-defined]


async def _asgi_get(app, *, path: str, client_ip: str, headers: list[tuple[bytes, bytes]] | None = None):
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii", errors="ignore"),
        "query_string": b"",
        "headers": headers or [],
        "client": (client_ip, 12345),
        "server": ("testserver", 80),
    }
    messages: list[dict[str, Any]] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next((m for m in messages if m.get("type") == "http.response.start"), None)
    if not start:
        raise RuntimeError("no_response_start")
    return int(start.get("status") or 0)


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    # Apply the same middleware shape as production.
    app.middleware("http")(remote_access_middleware)
    return app


async def _run() -> None:
    app = _build_app()

    # Scenario 1: tailscale mode should allow 100.64/10 and block public IPs
    _reset_settings_env({"REMOTE_ACCESS_MODE": "tailscale"})
    ok = await _asgi_get(app, path="/healthz", client_ip="100.100.100.100")
    bad = await _asgi_get(app, path="/healthz", client_ip="8.8.8.8")
    assert ok == 200, f"tailscale allowed IP should be 200, got {ok}"
    assert bad == 403, f"tailscale public IP should be 403, got {bad}"

    # Scenario 2: cloudflare mode should enforce peer allowlist (default private only)
    _reset_settings_env({"REMOTE_ACCESS_MODE": "cloudflare"})
    ok2 = await _asgi_get(app, path="/healthz", client_ip="172.16.0.10")
    bad2 = await _asgi_get(app, path="/healthz", client_ip="8.8.8.8")
    assert ok2 == 200, f"cloudflare private peer should be 200, got {ok2}"
    assert bad2 == 403, f"cloudflare public peer should be 403, got {bad2}"

    # Scenario 3: cloudflare mode with trusted proxy headers should accept proxied client IP
    _reset_settings_env(
        {
            "REMOTE_ACCESS_MODE": "cloudflare",
            "TRUST_PROXY_HEADERS": "1",
            "ALLOWED_SUBNETS": "127.0.0.0/8",
            "TRUSTED_PROXY_SUBNETS": "127.0.0.0/8",
        }
    )
    ok3 = await _asgi_get(
        app,
        path="/healthz",
        client_ip="127.0.0.1",
        headers=[(b"x-forwarded-for", b"100.99.88.77")],
    )
    assert ok3 == 200, f"cloudflare proxied request should be 200, got {ok3}"


def main() -> int:
    asyncio.run(_run())
    print("verify_remote_mode: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

