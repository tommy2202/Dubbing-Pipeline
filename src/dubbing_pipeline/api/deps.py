from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from dubbing_pipeline.api.models import AuthStore, Role, User
from dubbing_pipeline.api.security import decode_token, extract_api_key, extract_bearer, verify_csrf
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.crypto import verify_secret
from dubbing_pipeline.utils.log import set_user_id
from dubbing_pipeline.utils.ratelimit import RateLimiter


@dataclass(frozen=True, slots=True)
class Identity:
    kind: str  # user|api_key
    user: User
    scopes: list[str]
    api_key_prefix: str | None = None


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def get_store(request: Request) -> AuthStore:
    store = getattr(request.app.state, "auth_store", None)
    if store is None:
        raise HTTPException(status_code=500, detail="Auth store not initialized")
    return store


def get_limiter(request: Request) -> RateLimiter:
    rl = getattr(request.app.state, "rate_limiter", None)
    if rl is None:
        rl = RateLimiter()
        request.app.state.rate_limiter = rl
    return rl


def current_identity(request: Request, store: AuthStore = Depends(get_store)) -> Identity:
    s = get_settings()

    # 1) API key auth (automation): bypass CSRF but still RBAC/scopes
    api_key = extract_api_key(request) if bool(getattr(s, "enable_api_keys", True)) else None
    if api_key:
        # format: dp_<prefix>_<secret>
        parts = api_key.split("_", 2)
        if len(parts) != 3:
            raise HTTPException(status_code=401, detail="Invalid API key")
        _, prefix, secret = parts
        cands = store.find_api_keys_by_prefix(prefix)
        for k in cands:
            if verify_secret(k.key_hash, api_key):
                user = store.get_user(k.user_id)
                if user is None:
                    break
                set_user_id(user.id)
                return Identity(kind="api_key", user=user, scopes=k.scopes, api_key_prefix=prefix)
        raise HTTPException(status_code=401, detail="Invalid API key")

    # 2) Bearer access token (header); legacy `?token=...` is gated (unsafe on public networks).
    token = extract_bearer(request)
    if not token:
        allow_legacy = bool(getattr(s, "allow_legacy_token_login", False))
        if allow_legacy and hasattr(request, "query_params"):
            # LAN-only safety: accept query token only from private/loopback peers.
            try:
                import ipaddress

                peer = request.client.host if request.client else ""
                ip = ipaddress.ip_address(peer) if peer else None
                is_private = bool(ip and (ip.is_private or ip.is_loopback))
            except Exception:
                is_private = False
            if is_private:
                token = request.query_params.get("token")
    if token:
        data = decode_token(token, expected_typ="access")
        sub = str(data.get("sub") or "")
        user = store.get_user(sub)
        if user is None:
            raise HTTPException(status_code=401, detail="Unknown user")
        set_user_id(user.id)
        scopes = data.get("scopes") if isinstance(data.get("scopes"), list) else []
        scopes = [str(s) for s in scopes]
        return Identity(kind="user", user=user, scopes=scopes)

    # 3) Optional signed session cookie (web UI mode)
    sess = request.cookies.get("session")
    if sess:
        try:
            from itsdangerous import BadSignature, URLSafeTimedSerializer  # type: ignore

            ser = URLSafeTimedSerializer(s.session_secret.get_secret_value(), salt="session")
            token = ser.loads(sess, max_age=60 * 60 * 24 * 7)
            data = decode_token(str(token), expected_typ="access")
            sub = str(data.get("sub") or "")
            user = store.get_user(sub)
            if user is None:
                raise HTTPException(status_code=401, detail="Unknown user")
            set_user_id(user.id)
            scopes = data.get("scopes") if isinstance(data.get("scopes"), list) else []
            scopes = [str(x) for x in scopes]
            # CSRF is enforced for cookie sessions on state-changing requests by require_role/require_scope.
            return Identity(kind="user", user=user, scopes=scopes)
        except BadSignature:
            raise HTTPException(status_code=401, detail="Invalid session") from None

    raise HTTPException(status_code=401, detail="Not authenticated")


def require_role(min_role: Role):
    # Role ordering is used only for coarse UI/admin gating.
    # Fine-grained permissions should use require_scope.
    order = {Role.viewer: 0, Role.operator: 1, Role.editor: 1, Role.admin: 2}

    def dep(request: Request, ident: Identity = Depends(current_identity)) -> Identity:
        # CSRF: enforce for browser/cookie sessions on state-changing requests.
        # - API keys are exempt.
        # - Bearer-token API clients (no cookies, no Origin) are exempt.
        if request.method not in {"GET", "HEAD", "OPTIONS"} and ident.kind != "api_key":
            has_origin = bool(request.headers.get("origin"))
            uses_cookies = bool(request.cookies.get("session") or request.cookies.get("refresh"))
            if has_origin or uses_cookies:
                verify_csrf(request)
        if order[ident.user.role] < order[min_role]:
            raise HTTPException(status_code=403, detail="Forbidden")
        return ident

    return dep


def require_scope(scope: str):
    def dep(request: Request, ident: Identity = Depends(current_identity)) -> Identity:
        # CSRF: enforce for browser/cookie sessions on state-changing requests.
        # - API keys are exempt.
        # - Bearer-token API clients (no cookies, no Origin) are exempt.
        if request.method not in {"GET", "HEAD", "OPTIONS"} and ident.kind != "api_key":
            has_origin = bool(request.headers.get("origin"))
            uses_cookies = bool(request.cookies.get("session") or request.cookies.get("refresh"))
            if has_origin or uses_cookies:
                verify_csrf(request)

        if ident.user.role == Role.admin:
            return ident
        scopes = set(ident.scopes or [])
        if "admin:*" in scopes:
            return ident
        if scope not in scopes:
            raise HTTPException(status_code=403, detail="Insufficient scope")
        return ident

    return dep


def rate_limit(*, bucket: str, limit: int, per_seconds: int):
    def dep(
        request: Request, rl: RateLimiter = Depends(get_limiter), ident: Identity | None = None
    ):
        ip = _client_ip(request)
        who = ip
        try:
            if ident is None:
                # try to derive identity key if present
                # (do not raise if unauthenticated)
                pass
        except Exception:
            pass
        key = f"{bucket}:{who}"
        if not rl.allow(key, limit=limit, per_seconds=per_seconds):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")

    return dep
