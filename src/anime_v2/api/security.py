from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, status

from anime_v2.config import get_api_settings
from anime_v2.utils.crypto import random_id, verify_secret


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    csrf_token: str


def _jwt():
    try:
        import jwt  # type: ignore
    except Exception as ex:  # pragma: no cover
        raise RuntimeError("pyjwt not installed") from ex
    return jwt


def create_access_token(*, sub: str, role: str, scopes: list[str], minutes: int) -> str:
    s = get_api_settings()
    now = int(time.time())
    payload: dict[str, Any] = {
        "typ": "access",
        "sub": sub,
        "role": role,
        "scopes": scopes,
        "iat": now,
        "exp": now + int(minutes) * 60,
    }
    return _jwt().encode(payload, s.jwt_secret, algorithm=s.jwt_alg)


def create_refresh_token(*, sub: str, days: int) -> str:
    s = get_api_settings()
    now = int(time.time())
    payload: dict[str, Any] = {
        "typ": "refresh",
        "sub": sub,
        "iat": now,
        "exp": now + int(days) * 86400,
        "jti": random_id("r_", 16),
    }
    return _jwt().encode(payload, s.jwt_secret, algorithm=s.jwt_alg)


def decode_token(token: str, *, expected_typ: str) -> dict[str, Any]:
    s = get_api_settings()
    try:
        data = _jwt().decode(token, s.jwt_secret, algorithms=[s.jwt_alg])
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    if not isinstance(data, dict) or data.get("typ") != expected_typ:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")
    return data


def issue_csrf_token() -> str:
    # Signed CSRF token stored in cookie and echoed in header (double-submit).
    s = get_api_settings()
    try:
        from itsdangerous import URLSafeTimedSerializer  # type: ignore
    except Exception as ex:  # pragma: no cover
        raise RuntimeError("itsdangerous not installed") from ex
    raw = random_id("c_", 16)
    ser = URLSafeTimedSerializer(s.csrf_secret, salt="csrf")
    return ser.dumps(raw)


def verify_csrf(request: Request) -> None:
    """
    Enforce CSRF for state-changing requests when authenticated by cookie/session.
    Double-submit: header X-CSRF-Token must match csrf cookie, and token must validate.
    """
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    cookie = request.cookies.get("csrf") or ""
    header = request.headers.get("x-csrf-token") or ""
    if not cookie or not header or cookie != header:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF required")
    s = get_api_settings()
    try:
        from itsdangerous import BadSignature, URLSafeTimedSerializer  # type: ignore
    except Exception:  # pragma: no cover
        return
    ser = URLSafeTimedSerializer(s.csrf_secret, salt="csrf")
    try:
        ser.loads(cookie, max_age=60 * 60 * 24 * 7)
    except BadSignature:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF invalid")


def extract_bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip() or None
    return None


def extract_api_key(request: Request) -> str | None:
    # Either Authorization: Bearer <dp_...> or X-Api-Key
    k = request.headers.get("x-api-key")
    if k:
        return k.strip() or None
    b = extract_bearer(request)
    if b and b.startswith("dp_"):
        return b
    return None

