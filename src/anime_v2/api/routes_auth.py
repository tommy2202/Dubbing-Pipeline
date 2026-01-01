from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from anime_v2.api.middleware import audit_event
from anime_v2.api.models import AuthStore, Role, User, now_ts
from anime_v2.api.security import create_access_token, create_refresh_token, decode_token, issue_csrf_token
from anime_v2.config import get_settings
from anime_v2.utils.crypto import PasswordHasher, random_id
from anime_v2.utils.ratelimit import RateLimiter


router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _get_store(request: Request) -> AuthStore:
    s = getattr(request.app.state, "auth_store", None)
    if s is None:
        raise HTTPException(status_code=500, detail="Auth store not initialized")
    return s


def _get_rl(request: Request) -> RateLimiter:
    rl = getattr(request.app.state, "rate_limiter", None)
    if rl is None:
        rl = RateLimiter()
        request.app.state.rate_limiter = rl
    return rl


@router.post("/login")
async def login(request: Request) -> Response:
    rl = _get_rl(request)
    ip = _client_ip(request)
    if not rl.allow(f"auth:login:ip:{ip}", limit=5, per_seconds=60):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    body: dict[str, Any] = {}
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        raw = await request.json()
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON")
        body = raw
    elif "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
        form = await request.form()
        body = {str(k): form.get(k) for k in form.keys()}
    else:
        # best-effort: try JSON
        try:
            raw = await request.json()
            if isinstance(raw, dict):
                body = raw
        except Exception:
            body = {}

    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    totp = str(body.get("totp") or "").strip() or None
    session_val = body.get("session") or False
    if isinstance(session_val, str):
        session = session_val.strip().lower() in {"1", "true", "yes", "on"}
    else:
        session = bool(session_val)

    store = _get_store(request)
    user = store.get_user_by_username(username)
    if user is None:
        audit_event("auth.login_failed", request=request, user_id=None, meta={"username": username})
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not PasswordHasher().verify(user.password_hash, password):
        audit_event("auth.login_failed", request=request, user_id=None, meta={"username": username})
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if user.totp_enabled:
        if not totp:
            audit_event("auth.login_failed_totp", request=request, user_id=user.id, meta={"username": username})
            raise HTTPException(status_code=401, detail="TOTP required")
        try:
            import pyotp  # type: ignore

            if not user.totp_secret or not pyotp.TOTP(user.totp_secret).verify(totp, valid_window=1):
                audit_event("auth.login_failed_totp", request=request, user_id=user.id, meta={"username": username})
                raise HTTPException(status_code=401, detail="Invalid TOTP")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=500, detail="TOTP unavailable")

    s = get_settings()
    access = create_access_token(sub=user.id, role=user.role.value, scopes=["read:job"], minutes=s.access_token_minutes)
    # role-based default scopes (viewer=read, operator=read+submit, admin implicit)
    scopes = ["read:job"]
    if user.role in {Role.operator, Role.admin}:
        scopes.append("submit:job")
    if user.role == Role.admin:
        scopes.append("admin:*")
    access = create_access_token(sub=user.id, role=user.role.value, scopes=scopes, minutes=s.access_token_minutes)

    refresh = create_refresh_token(sub=user.id, days=s.refresh_token_days)
    csrf = issue_csrf_token()
    audit_event("auth.login_ok", request=request, user_id=user.id, meta={"username": username, "role": user.role.value, "session": session})

    resp = Response()
    resp.set_cookie(
        "refresh",
        refresh,
        httponly=True,
        samesite="lax",
        secure=s.cookie_secure,
        max_age=s.refresh_token_days * 86400,
        path="/",
    )
    resp.set_cookie(
        "csrf",
        csrf,
        httponly=False,
        samesite="lax",
        secure=s.cookie_secure,
        max_age=s.refresh_token_days * 86400,
        path="/",
    )

    if session:
        try:
            from itsdangerous import URLSafeTimedSerializer  # type: ignore

            ser = URLSafeTimedSerializer(s.session_secret.get_secret_value(), salt="session")
            signed = ser.dumps(access)
            resp.set_cookie(
                "session",
                signed,
                httponly=True,
                samesite="lax",
                secure=s.cookie_secure,
                max_age=s.refresh_token_days * 86400,
                path="/",
            )
        except Exception:
            pass

    resp.headers["content-type"] = "application/json"
    resp.body = __import__("json").dumps(  # type: ignore[attr-defined]
        {"access_token": access, "token_type": "bearer", "csrf_token": csrf, "role": user.role.value}
    ).encode("utf-8")
    return resp


@router.post("/refresh")
async def refresh(request: Request) -> Response:
    rl = _get_rl(request)
    ip = _client_ip(request)
    if not rl.allow(f"auth:refresh:ip:{ip}", limit=5, per_seconds=60):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    rt = request.cookies.get("refresh")
    if not rt:
        audit_event("auth.refresh_failed", request=request, user_id=None, meta={"reason": "missing_refresh_cookie"})
        raise HTTPException(status_code=401, detail="Missing refresh token")
    data = decode_token(rt, expected_typ="refresh")
    sub = str(data.get("sub") or "")
    store = _get_store(request)
    user = store.get_user(sub)
    if user is None:
        audit_event("auth.refresh_failed", request=request, user_id=None, meta={"reason": "unknown_user"})
        raise HTTPException(status_code=401, detail="Unknown user")

    s = get_settings()
    scopes = ["read:job"]
    if user.role in {Role.operator, Role.admin}:
        scopes.append("submit:job")
    if user.role == Role.admin:
        scopes.append("admin:*")
    access = create_access_token(sub=user.id, role=user.role.value, scopes=scopes, minutes=s.access_token_minutes)
    csrf = issue_csrf_token()
    audit_event("auth.refresh_ok", request=request, user_id=user.id, meta={"role": user.role.value})

    resp = Response()
    resp.set_cookie(
        "csrf",
        csrf,
        httponly=False,
        samesite="lax",
        secure=s.cookie_secure,
        max_age=s.refresh_token_days * 86400,
        path="/",
    )
    resp.headers["content-type"] = "application/json"
    resp.body = __import__("json").dumps(  # type: ignore[attr-defined]
        {"access_token": access, "token_type": "bearer", "csrf_token": csrf}
    ).encode("utf-8")
    return resp


@router.post("/logout")
async def logout() -> Response:
    resp = Response(content=b'{"ok":true}', media_type="application/json")
    resp.delete_cookie("refresh", path="/")
    resp.delete_cookie("csrf", path="/")
    resp.delete_cookie("session", path="/")
    return resp


@router.post("/totp/setup")
async def totp_setup(request: Request) -> dict[str, Any]:
    # Requires bearer auth
    from anime_v2.api.deps import current_identity

    ident = current_identity(request, _get_store(request))
    user = ident.user
    try:
        import pyotp  # type: ignore
    except Exception:
        raise HTTPException(status_code=500, detail="TOTP unavailable")
    secret = pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.username, issuer_name="anime_v2")
    # Store secret but not enabled until verified
    _get_store(request).set_totp(user.id, secret=secret, enabled=False)
    return {"secret": secret, "uri": uri}


@router.post("/totp/verify")
async def totp_verify(request: Request) -> dict[str, Any]:
    from anime_v2.api.deps import current_identity

    ident = current_identity(request, _get_store(request))
    user = _get_store(request).get_user(ident.user.id)
    if user is None or not user.totp_secret:
        raise HTTPException(status_code=400, detail="TOTP not initialized")
    body = await request.json()
    code = str((body or {}).get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="Missing code")
    try:
        import pyotp  # type: ignore

        if not pyotp.TOTP(user.totp_secret).verify(code, valid_window=1):
            raise HTTPException(status_code=400, detail="Invalid code")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="TOTP unavailable")
    _get_store(request).set_totp(user.id, secret=user.totp_secret, enabled=True)
    return {"ok": True}

