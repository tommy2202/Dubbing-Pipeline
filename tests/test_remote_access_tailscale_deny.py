from __future__ import annotations

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.server import app
from tests._helpers.runtime_paths import configure_runtime_paths


def _configure_remote_env(monkeypatch, tmp_path) -> None:
    configure_runtime_paths(tmp_path)
    monkeypatch.setenv("ACCESS_MODE", "tailscale")
    monkeypatch.setenv("ALLOWED_SUBNETS", "100.64.0.0/10")
    monkeypatch.setenv("TRUST_PROXY_HEADERS_FOR_TESTS", "1")
    get_settings.cache_clear()


def test_tailscale_denies_non_allowed_ip(tmp_path, monkeypatch) -> None:
    _configure_remote_env(monkeypatch, tmp_path)
    with TestClient(app) as c:
        resp = c.get(
            "/health",
            headers={
                "x-test-peer-ip": "1.2.3.4",
                "x-forwarded-for": "1.2.3.4",
            },
        )
        assert resp.status_code == 403, resp.text


def test_tailscale_allows_tailscale_ip(tmp_path, monkeypatch) -> None:
    _configure_remote_env(monkeypatch, tmp_path)
    with TestClient(app) as c:
        resp = c.get(
            "/health",
            headers={
                "x-test-peer-ip": "100.100.100.100",
                "x-forwarded-for": "100.100.100.100",
            },
        )
        assert resp.status_code == 200, resp.text
