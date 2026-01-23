from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from dubbing_pipeline.api.auth.refresh_tokens import (
    RefreshTokenError,
    issue_and_store_refresh_token,
    revoke_refresh_token_best_effort,
    rotate_refresh_token,
)
from dubbing_pipeline.api.middleware import audit_event
from dubbing_pipeline.api.models import AuthStore, Role
from dubbing_pipeline.api.security import (
    create_access_token,
    decode_token,
    issue_csrf_token,
    verify_csrf,
)
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.crypto import PasswordHasher
from dubbing_pipeline.utils.net import get_client_ip
from dubbing_pipeline.utils.ratelimit import RateLimiter

router = APIRouter(prefix="/auth", tags=["auth"])

# Safe import: deps only depends on models/security; no circular back to routes_auth.
from dubbing_pipeline.api.deps import Identity, require_role  # noqa: E402


def _client_ip(request: Request) -> str:
    return get_client_ip(request)


def _ua(request: Request) -> str:
    return str(request.headers.get("user-agent") or "")[:160]


def _device_name_guess(request: Request) -> str:
    # Simple, privacy-preserving device label.
    ua = (request.headers.get("user-agent") or "").strip()
    if not ua:
        return "browser"
    low = ua.lower()
    if "iphone" in low or "ipad" in low:
        return "iOS"
    if "android" in low:
        return "Android"
    if "windows" in low:
        return "Windows"
    if "mac os" in low or "macintosh" in low:
        return "macOS"
    if "linux" in low:
        return "Linux"
    return "browser"


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
        body = {str(k): form.get(k) for k in form}
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
    recovery_code = str(body.get("recovery_code") or "").strip() or None
    session_val = body.get("session") or False
    if isinstance(session_val, str):
        session = session_val.strip().lower() in {"1", "true", "yes", "on"}
    else:
        session = bool(session_val)

    # Brute-force protection: also limit by username (best-effort, avoids user enumeration by using same error msg).
    if username and not rl.allow(f"auth:login:user:{username.lower()}", limit=5, per_seconds=60):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    store = _get_store(request)
    user = store.get_user_by_username(username)
    if user is None:
        audit_event("auth.login_failed", request=request, user_id=None, meta={"username": username})
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not PasswordHasher().verify(user.password_hash, password):
        audit_event("auth.login_failed", request=request, user_id=None, meta={"username": username})
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Optional TOTP (admin only) when enabled by config.
    s = get_settings()
    enforce_totp = (
        bool(getattr(s, "enable_totp", False))
        and (user.role == Role.admin)
        and bool(user.totp_enabled)
    )
    if enforce_totp:
        if not totp:
            # recovery code fallback (optional)
            if recovery_code:
                try:
                    import hashlib as _hashlib

                    h = _hashlib.sha256(str(recovery_code).encode("utf-8")).hexdigest()
                    if store.consume_recovery_code(user_id=user.id, code_hash=h):
                        totp = "recovery_used"
                    else:
                        raise HTTPException(status_code=401, detail="Invalid recovery code")
                except HTTPException:
                    raise
                except Exception as ex:
                    raise HTTPException(
                        status_code=500, detail="Recovery codes unavailable"
                    ) from ex
            else:
                audit_event(
                    "auth.login_failed_totp",
                    request=request,
                    user_id=user.id,
                    meta={"username": username},
                )
                raise HTTPException(status_code=401, detail="TOTP required")
        if totp != "recovery_used":
            audit_event(
                "auth.login_failed_totp",
                request=request,
                user_id=user.id,
                meta={"username": username},
            )
            try:
                import pyotp  # type: ignore

                from dubbing_pipeline.security.field_crypto import decrypt_field

                if not user.totp_secret:
                    raise HTTPException(status_code=500, detail="TOTP misconfigured")
                secret = decrypt_field(str(user.totp_secret), aad=f"totp:{user.id}")
                if not pyotp.TOTP(secret).verify(str(totp), valid_window=1):
                    audit_event(
                        "auth.login_failed_totp",
                        request=request,
                        user_id=user.id,
                        meta={"username": username},
                    )
                    raise HTTPException(status_code=401, detail="Invalid TOTP")
            except HTTPException:
                raise
            except Exception as ex:
                raise HTTPException(status_code=500, detail="TOTP unavailable") from ex
    elif (
        bool(user.totp_enabled)
        and not bool(getattr(s, "enable_totp", False))
        and user.role == Role.admin
    ):
        # If a DB has totp_enabled set but feature is disabled, fail safe for admin.
        raise HTTPException(status_code=500, detail="TOTP is disabled by server policy")

    access = create_access_token(
        sub=user.id, role=user.role.value, scopes=["read:job"], minutes=s.access_token_minutes
    )
    # role-based default scopes
    # - viewer: read-only
    # - operator: submit jobs
    # - editor: segment edits/overrides (but not job submission)
    # - admin: all
    scopes = ["read:job"]
    if user.role in {Role.operator, Role.admin}:
        scopes.append("submit:job")
    if user.role in {Role.editor, Role.admin}:
        scopes.append("edit:job")
    if user.role == Role.admin:
        scopes.append("admin:*")
    access = create_access_token(
        sub=user.id, role=user.role.value, scopes=scopes, minutes=s.access_token_minutes
    )

    csrf = issue_csrf_token()
    audit_event(
        "auth.login_ok",
        request=request,
        user_id=user.id,
        meta={"username": username, "role": user.role.value, "session": session},
    )

    resp = Response()
    resp.set_cookie(
        "refresh",
        issue_and_store_refresh_token(
            store=store,
            user_id=user.id,
            days=s.refresh_token_days,
            device_id=__import__("secrets").token_hex(8),
            device_name=_device_name_guess(request),
            created_ip=_client_ip(request),
            user_agent=_ua(request),
        ),
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
    resp.body = (
        __import__("json")
        .dumps(  # type: ignore[attr-defined]
            {
                "access_token": access,
                "token_type": "bearer",
                "csrf_token": csrf,
                "role": user.role.value,
            }
        )
        .encode("utf-8")
    )
    return resp


@router.post("/refresh")
async def refresh(request: Request) -> Response:
    rl = _get_rl(request)
    ip = _client_ip(request)
    if not rl.allow(f"auth:refresh:ip:{ip}", limit=5, per_seconds=60):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    rt = request.cookies.get("refresh")
    if not rt:
        audit_event(
            "auth.refresh_failed",
            request=request,
            user_id=None,
            meta={"reason": "missing_refresh_cookie"},
        )
        raise HTTPException(status_code=401, detail="Missing refresh token")
    # Cookie flow: require CSRF (double-submit)
    verify_csrf(request)

    store = _get_store(request)
    try:
        rot = rotate_refresh_token(
            store=store,
            refresh_token=str(rt),
            days=get_settings().refresh_token_days,
            used_ip=_client_ip(request),
        )
        user = store.get_user(rot.access_sub)
        if user is None:
            audit_event(
                "auth.refresh_failed",
                request=request,
                user_id=None,
                meta={"reason": "unknown_user"},
            )
            raise HTTPException(status_code=401, detail="Unknown user")
    except RefreshTokenError as ex:
        audit_event(
            "auth.refresh_failed",
            request=request,
            user_id=None,
            meta={"reason": str(ex)},
        )
        raise HTTPException(status_code=401, detail="Invalid refresh token") from None

    s = get_settings()
    scopes = ["read:job"]
    if user.role in {Role.operator, Role.admin}:
        scopes.append("submit:job")
    if user.role in {Role.editor, Role.admin}:
        scopes.append("edit:job")
    if user.role == Role.admin:
        scopes.append("admin:*")
    access = create_access_token(
        sub=user.id, role=user.role.value, scopes=scopes, minutes=s.access_token_minutes
    )
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
    resp.set_cookie(
        "refresh",
        rot.new_refresh_token,
        httponly=True,
        samesite="lax",
        secure=s.cookie_secure,
        max_age=s.refresh_token_days * 86400,
        path="/",
    )
    resp.headers["content-type"] = "application/json"
    resp.body = (
        __import__("json")
        .dumps(  # type: ignore[attr-defined]
            {"access_token": access, "token_type": "bearer", "csrf_token": csrf}
        )
        .encode("utf-8")
    )
    return resp


@router.post("/logout")
async def logout(request: Request) -> Response:
    # Cookie flow: require CSRF (double-submit) when cookies are present.
    # (Clients using pure Bearer tokens can ignore logout; they can just drop the token.)
    verify_csrf(request)
    store = _get_store(request)
    rt = request.cookies.get("refresh") or ""
    uid = None
    if rt:
        try:
            data = decode_token(str(rt), expected_typ="refresh")
            uid = str(data.get("sub") or "") or None
        except Exception:
            uid = None
        revoke_refresh_token_best_effort(store=store, refresh_token=str(rt))
    audit_event("auth.logout", request=request, user_id=uid, meta=None)
    resp = Response(content=b'{"ok":true}', media_type="application/json")
    resp.delete_cookie("refresh", path="/")
    resp.delete_cookie("csrf", path="/")
    resp.delete_cookie("session", path="/")
    return resp


@router.post("/totp/setup")
async def totp_setup(request: Request) -> dict[str, Any]:
    # Requires bearer auth
    from dubbing_pipeline.api.deps import current_identity

    ident = current_identity(request, _get_store(request))
    user = ident.user
    if user.role != Role.admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not bool(getattr(get_settings(), "enable_totp", False)):
        raise HTTPException(status_code=404, detail="TOTP disabled")
    try:
        import pyotp  # type: ignore
    except Exception as ex:
        raise HTTPException(status_code=500, detail="TOTP unavailable") from ex
    secret = pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.username, issuer_name="dubbing_pipeline")
    # Store secret but not enabled until verified
    try:
        from dubbing_pipeline.security.field_crypto import encrypt_field

        enc = encrypt_field(secret, aad=f"totp:{user.id}")
    except Exception as ex:
        raise HTTPException(status_code=500, detail="TOTP encryption unavailable") from ex
    _get_store(request).set_totp(user.id, secret=enc, enabled=False)

    # Optional: generate recovery codes now (returned once).
    codes = []
    try:
        import hashlib as _hashlib
        import secrets as _secrets

        codes = [
            (_secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:10]) for _ in range(8)
        ]
        hashes = [_hashlib.sha256(c.encode("utf-8")).hexdigest() for c in codes]
        _get_store(request).put_recovery_codes(user_id=user.id, code_hashes=hashes)
    except Exception:
        codes = []
    return {"secret": secret, "uri": uri, "recovery_codes": codes}


@router.post("/totp/verify")
async def totp_verify(request: Request) -> dict[str, Any]:
    from dubbing_pipeline.api.deps import current_identity

    ident = current_identity(request, _get_store(request))
    user = _get_store(request).get_user(ident.user.id)
    if user is None or not user.totp_secret:
        raise HTTPException(status_code=400, detail="TOTP not initialized")
    if user.role != Role.admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not bool(getattr(get_settings(), "enable_totp", False)):
        raise HTTPException(status_code=404, detail="TOTP disabled")
    body = await request.json()
    code = str((body or {}).get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="Missing code")
    try:
        import pyotp  # type: ignore

        from dubbing_pipeline.security.field_crypto import decrypt_field

        secret = decrypt_field(str(user.totp_secret), aad=f"totp:{user.id}")
        if not pyotp.TOTP(secret).verify(code, valid_window=1):
            raise HTTPException(status_code=400, detail="Invalid code")
    except HTTPException:
        raise
    except Exception as ex:
        raise HTTPException(status_code=500, detail="TOTP unavailable") from ex
    _get_store(request).set_totp(user.id, secret=user.totp_secret, enabled=True)
    return {"ok": True}


@router.post("/qr/init")
async def qr_init(
    request: Request,
    ident: Identity = Depends(require_role(Role.admin)),
) -> dict[str, Any]:
    """
    Admin-only: mint a short-lived, single-use QR login code.
    The QR contains ONLY the nonce (no password/token). Redeeming it sets session cookies.
    """
    s = get_settings()
    if not bool(getattr(s, "enable_qr_login", False)):
        raise HTTPException(status_code=404, detail="QR login disabled")
    ttl = max(10, min(300, int(getattr(s, "qr_login_ttl_sec", 60) or 60)))

    import hashlib
    import secrets

    code = "qr_" + secrets.token_urlsafe(18)
    nonce_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    now = __import__("time").time()
    created_at = int(now)
    expires_at = int(now) + int(ttl)
    store = _get_store(request)
    store.put_qr_code(
        nonce_hash=nonce_hash,
        user_id=str(ident.user.id),
        created_at=created_at,
        expires_at=expires_at,
        created_ip=_client_ip(request),
    )

    base = str(getattr(s, "public_base_url", "") or "").strip().rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    from urllib.parse import quote

    redeem_url = f"{base}/ui/qr?code={quote(code)}"

    audit_event(
        "auth.qr_init",
        request=request,
        user_id=ident.user.id,
        meta={"ttl_sec": ttl},
    )
    return {"code": code, "expires_at": expires_at, "redeem_url": redeem_url}


@router.post("/qr/redeem")
async def qr_redeem(request: Request) -> Response:
    """
    Unauthenticated: exchange a one-time code for a session (cookies).
    Single-use and short-lived; does not reveal reusable secrets.
    """
    s = get_settings()
    if not bool(getattr(s, "enable_qr_login", False)):
        raise HTTPException(status_code=404, detail="QR login disabled")
    rl = _get_rl(request)
    ip = _client_ip(request)
    if not rl.allow(f"auth:qr:redeem:ip:{ip}", limit=10, per_seconds=60):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    code = str(body.get("code") or "").strip()
    if not code or not code.startswith("qr_"):
        raise HTTPException(status_code=400, detail="Invalid code")

    import hashlib

    nonce_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    store = _get_store(request)
    uid = store.consume_qr_code(nonce_hash=nonce_hash, used_ip=ip)
    if not uid:
        audit_event(
            "auth.qr_redeem_failed", request=request, user_id=None, meta={"reason": "invalid"}
        )
        raise HTTPException(status_code=401, detail="Invalid or expired code")

    user = store.get_user(str(uid))
    if user is None:
        raise HTTPException(status_code=401, detail="Unknown user")

    scopes = ["read:job"]
    if user.role in {Role.operator, Role.admin}:
        scopes.append("submit:job")
    if user.role == Role.admin:
        scopes.append("admin:*")
    access = create_access_token(
        sub=user.id, role=user.role.value, scopes=scopes, minutes=s.access_token_minutes
    )
    csrf = issue_csrf_token()

    resp = Response()
    resp.set_cookie(
        "refresh",
        issue_and_store_refresh_token(
            store=store,
            user_id=user.id,
            days=s.refresh_token_days,
            device_id=__import__("secrets").token_hex(8),
            device_name="qr-login",
            created_ip=ip,
            user_agent=_ua(request),
        ),
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
    resp.body = __import__("json").dumps({"ok": True, "csrf_token": csrf}).encode("utf-8")
    audit_event(
        "auth.qr_redeem_ok", request=request, user_id=user.id, meta={"role": user.role.value}
    )
    return resp


@router.get("/sessions")
async def list_sessions(request: Request) -> dict[str, Any]:
    from dubbing_pipeline.api.deps import current_identity

    ident = current_identity(request, _get_store(request))
    store = _get_store(request)
    items = store.list_active_sessions(user_id=str(ident.user.id))
    # Stable, minimal payload.
    out = []
    for it in items:
        out.append(
            {
                "device_id": str(it.get("device_id") or "") or str(it.get("jti") or ""),
                "device_name": str(it.get("device_name") or "") or "",
                "created_at": int(it.get("created_at") or 0),
                "last_used_at": (
                    int(it.get("last_used_at") or 0) if it.get("last_used_at") else None
                ),
                "created_ip": str(it.get("created_ip") or "") or "",
                "last_ip": str(it.get("last_ip") or "") or "",
                "user_agent": str(it.get("user_agent") or "") or "",
                "expires_at": int(it.get("expires_at") or 0),
            }
        )
    return {"items": out}


@router.post("/sessions/{device_id}/revoke")
async def revoke_session(request: Request, device_id: str) -> dict[str, Any]:
    from dubbing_pipeline.api.deps import current_identity

    ident = current_identity(request, _get_store(request))
    verify_csrf(request)
    n = _get_store(request).revoke_sessions_by_device(
        user_id=str(ident.user.id), device_id=str(device_id)
    )
    audit_event("auth.session_revoke", request=request, user_id=ident.user.id, meta={"count": n})
    return {"ok": True, "revoked": int(n)}


@router.post("/sessions/revoke_all")
async def revoke_all_sessions(request: Request) -> dict[str, Any]:
    from dubbing_pipeline.api.deps import current_identity

    ident = current_identity(request, _get_store(request))
    verify_csrf(request)
    _get_store(request).revoke_all_refresh_tokens_for_user(str(ident.user.id))
    audit_event("auth.session_revoke_all", request=request, user_id=ident.user.id, meta=None)
    return {"ok": True}


@router.api_route("/register", methods=["GET", "POST"])
async def register_disabled() -> dict[str, Any]:
    # Invite-only access: self-registration is disabled.
    raise HTTPException(status_code=404, detail="Registration disabled")


@router.api_route("/signup", methods=["GET", "POST"])
async def signup_disabled() -> dict[str, Any]:
    # Invite-only access: self-registration is disabled.
    raise HTTPException(status_code=404, detail="Registration disabled")
