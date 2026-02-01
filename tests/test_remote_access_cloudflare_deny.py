from __future__ import annotations

from fastapi.testclient import TestClient

from dubbing_pipeline.api import remote_access
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.server import app
from tests._helpers.runtime_paths import configure_runtime_paths


def _configure_remote_env(monkeypatch, tmp_path) -> None:
    configure_runtime_paths(tmp_path)
    monkeypatch.setenv("ACCESS_MODE", "cloudflare")
    monkeypatch.setenv("CLOUDFLARE_ACCESS_TEAM_DOMAIN", "example-team")
    monkeypatch.setenv("CLOUDFLARE_ACCESS_AUD", "example-aud")
    monkeypatch.setenv("TRUST_PROXY_HEADERS_FOR_TESTS", "1")
    get_settings.cache_clear()


def test_cloudflare_missing_header_denied(tmp_path, monkeypatch) -> None:
    _configure_remote_env(monkeypatch, tmp_path)
    remote_access._JWKS_CACHE["jwks"] = None
    remote_access._JWKS_CACHE["ts"] = 0.0
    with TestClient(app) as c:
        resp = c.get("/health")
        assert resp.status_code == 403, resp.text
        data = resp.json()
        assert data.get("detail") == "Forbidden"
        assert data.get("reason") == "missing_cf_access_jwt"


def test_cloudflare_invalid_token_denied(tmp_path, monkeypatch) -> None:
    _configure_remote_env(monkeypatch, tmp_path)
    remote_access._JWKS_CACHE["jwks"] = None
    remote_access._JWKS_CACHE["ts"] = 0.0

    def _fake_load(_team_domain: str, *, max_age_sec: int = 86400) -> dict:
        return {"keys": []}

    monkeypatch.setattr(remote_access, "_load_cf_access_jwks", _fake_load)
    with TestClient(app) as c:
        resp = c.get("/health", headers={"cf-access-jwt-assertion": "not-a-token"})
        assert resp.status_code == 403, resp.text
        data = resp.json()
        assert data.get("detail") == "Forbidden"
        assert str(data.get("reason") or "").startswith("invalid_cf_access_jwt")
