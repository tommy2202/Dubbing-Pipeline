from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True, slots=True)
class ApiSettings:
    jwt_secret: str
    jwt_alg: str
    csrf_secret: str
    session_secret: str

    access_token_minutes: int
    refresh_token_days: int

    cors_origins: list[str]
    cookie_secure: bool

    redis_url: str | None

    # bootstrap
    admin_username: str | None
    admin_password: str | None


def _env(key: str, default: str | None = None) -> str | None:
    v = os.environ.get(key)
    if v is None:
        return default
    v = v.strip()
    return v if v else default


def _env_bool(key: str, default: bool = False) -> bool:
    v = (_env(key, None) or "").lower()
    if not v:
        return default
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    return default


@lru_cache(maxsize=1)
def get_api_settings() -> ApiSettings:
    cors = _env("CORS_ORIGINS", "") or ""
    cors_origins = [o.strip() for o in cors.split(",") if o.strip()]

    return ApiSettings(
        jwt_secret=_env("JWT_SECRET", "dev-insecure-jwt-secret") or "dev-insecure-jwt-secret",
        jwt_alg=_env("JWT_ALG", "HS256") or "HS256",
        csrf_secret=_env("CSRF_SECRET", "dev-insecure-csrf-secret") or "dev-insecure-csrf-secret",
        session_secret=_env("SESSION_SECRET", "dev-insecure-session-secret") or "dev-insecure-session-secret",
        access_token_minutes=int(_env("ACCESS_TOKEN_MINUTES", "15") or "15"),
        refresh_token_days=int(_env("REFRESH_TOKEN_DAYS", "7") or "7"),
        cors_origins=cors_origins,
        cookie_secure=_env_bool("COOKIE_SECURE", default=False),
        redis_url=_env("REDIS_URL", None),
        admin_username=_env("ADMIN_USERNAME", None),
        admin_password=_env("ADMIN_PASSWORD", None),
    )

