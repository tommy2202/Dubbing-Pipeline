from __future__ import annotations

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.server import app
from tests._helpers.runtime_paths import configure_runtime_paths


def _configure_remote_env(monkeypatch, tmp_path) -> None:
    configure_runtime_paths(tmp_path)
    monkeypatch.setenv("ACCESS_MODE", "tailscale")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    monkeypatch.setenv("TRUSTED_PROXY_SUBNETS", "127.0.0.1/8")
    get_settings.cache_clear()


def test_tailscale_denies_non_tailscale_ip(tmp_path, monkeypatch) -> None:
    _configure_remote_env(monkeypatch, tmp_path)
    with TestClient(app) as c:
        resp = c.get("/health", headers={"x-forwarded-for": "1.2.3.4"})
        assert resp.status_code == 403, resp.text
        data = resp.json()
        assert data.get("detail") == "Forbidden"
        assert data.get("reason") == "client_ip_not_in_allowed_subnets"


def test_tailscale_allows_tailscale_ip(tmp_path, monkeypatch) -> None:
    _configure_remote_env(monkeypatch, tmp_path)
    with TestClient(app) as c:
        resp = c.get("/health", headers={"x-forwarded-for": "100.64.10.5"})
        assert resp.status_code == 200, resp.text
