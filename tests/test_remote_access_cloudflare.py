from __future__ import annotations

import json
import time

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
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    monkeypatch.setenv("TRUSTED_PROXY_SUBNETS", "127.0.0.1/8")
    get_settings.cache_clear()


def _make_jwks_and_token() -> tuple[dict, str]:
    import jwt  # type: ignore
    from cryptography.hazmat.primitives.asymmetric import rsa

    team = "example-team"
    aud = "example-aud"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = key.public_key()
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public_key))
    jwk["kid"] = "test-key"
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    jwks = {"keys": [jwk]}
    now = int(time.time())
    payload = {
        "aud": aud,
        "iss": f"https://{team}.cloudflareaccess.com",
        "iat": now,
        "exp": now + 300,
        "sub": "user_1",
    }
    token = jwt.encode(payload, key, algorithm="RS256", headers={"kid": "test-key"})
    return jwks, token


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
    jwks, _token = _make_jwks_and_token()
    remote_access._JWKS_CACHE["jwks"] = None
    remote_access._JWKS_CACHE["ts"] = 0.0

    def _fake_load(_team_domain: str, *, max_age_sec: int = 86400) -> dict:
        return jwks

    monkeypatch.setattr(remote_access, "_load_cf_access_jwks", _fake_load)
    with TestClient(app) as c:
        resp = c.get("/health", headers={"cf-access-jwt-assertion": "not-a-token"})
        assert resp.status_code == 403, resp.text
        data = resp.json()
        assert data.get("detail") == "Forbidden"
        assert str(data.get("reason") or "").startswith("invalid_cf_access_jwt")


def test_cloudflare_valid_token_allowed(tmp_path, monkeypatch) -> None:
    _configure_remote_env(monkeypatch, tmp_path)
    jwks, token = _make_jwks_and_token()
    remote_access._JWKS_CACHE["jwks"] = None
    remote_access._JWKS_CACHE["ts"] = 0.0

    def _fake_load(_team_domain: str, *, max_age_sec: int = 86400) -> dict:
        return jwks

    monkeypatch.setattr(remote_access, "_load_cf_access_jwks", _fake_load)
    with TestClient(app) as c:
        resp = c.get("/health", headers={"cf-access-jwt-assertion": token})
        assert resp.status_code == 200, resp.text
