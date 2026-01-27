from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request, status

from dubbing_pipeline.api import remote_access
from dubbing_pipeline.api.deps import Identity, current_identity, get_store
from dubbing_pipeline.api.middleware import audit_event
from dubbing_pipeline.api.models import AuthStore, User
from dubbing_pipeline.security import quotas, visibility
from dubbing_pipeline.utils.net import get_client_ip

_SAFE_META_KEYS = {
    "action",
    "reason",
    "mode",
    "access_mode",
    "scope",
    "visibility",
    "job_id",
    "upload_id",
    "user_id",
    "target_id",
    "resource_id",
    "bytes",
    "count",
    "limit",
    "current",
    "duration_s",
    "method",
    "path",
}


@dataclass(frozen=True, slots=True)
class PolicyContext:
    user: User | None
    roles: list[str]
    request_id: str | None
    client_ip: str | None
    access_mode: str | None
    method: str | None
    path: str | None

    @classmethod
    def from_request(
        cls, request: Request, *, identity: Identity | None = None
    ) -> "PolicyContext":
        user = identity.user if identity is not None else None
        role = None
        if user is not None:
            role = getattr(user.role, "value", user.role)
        roles = [str(role)] if role is not None else []
        request_id = (
            getattr(getattr(request, "state", None), "request_id", None)
            or request.headers.get("x-request-id")
            or None
        )
        client_ip = get_client_ip(request)
        posture = remote_access.resolve_access_posture()
        access_mode = str(posture.get("mode") or "").strip().lower() or None
        return cls(
            user=user,
            roles=roles,
            request_id=request_id,
            client_ip=client_ip,
            access_mode=access_mode,
            method=str(getattr(request, "method", "") or ""),
            path=str(getattr(getattr(request, "url", None), "path", "") or ""),
        )


class PolicyError(HTTPException):
    @classmethod
    def unauthorized(cls, detail: Any = "Not authenticated") -> "PolicyError":
        return cls(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)

    @classmethod
    def forbidden(cls, detail: Any = "Forbidden") -> "PolicyError":
        return cls(status_code=status.HTTP_403_FORBIDDEN, detail=detail)

    @classmethod
    def too_many(cls, detail: Any = "Too many requests") -> "PolicyError":
        return cls(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)


def _safe_meta(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    if not meta:
        return None
    safe: dict[str, Any] = {}
    for key, value in meta.items():
        if key not in _SAFE_META_KEYS:
            continue
        if value is None:
            continue
        if isinstance(value, (int, float, bool)):
            safe[key] = value
            continue
        if isinstance(value, str):
            safe[key] = value[:256]
            continue
    return safe or None


def audit_policy_event(
    event: str,
    *,
    request: Request,
    user: User | None = None,
    outcome: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    audit_event(
        event,
        request=request,
        user_id=str(user.id) if user is not None else None,
        outcome=outcome,
        meta_safe=_safe_meta(meta),
    )


def require_invite_only(*, user: User | None) -> None:
    if user is None:
        raise PolicyError.unauthorized()


def require_remote_access(*, request: Request) -> None:
    decision = remote_access.decide_remote_access(request)
    if decision.allowed:
        return
    raise PolicyError.forbidden(
        {"detail": "Forbidden", "reason": decision.reason, "mode": decision.mode}
    )


def require_can_view_job(*, user: User, job, allow_shared_read: bool = False) -> None:
    visibility.require_can_view_job(user=user, job=job, allow_shared_read=allow_shared_read)


async def require_quota(
    *, request: Request | None, user: User, action: str, bytes: int = 0
) -> None:
    if request is None or int(bytes) <= 0:
        return
    enforcer = quotas.QuotaEnforcer.from_request(request=request, user=user)
    await enforcer.require_upload_bytes(total_bytes=int(bytes), action=str(action))


def _resolve_identity(request: Request, store: AuthStore) -> Identity:
    ident = current_identity(request, store)
    try:
        request.state.identity = ident
    except Exception:
        pass
    return ident


def require_authenticated_user(
    request: Request, store: AuthStore = Depends(get_store)
) -> PolicyContext:
    ident = _resolve_identity(request, store)
    return PolicyContext.from_request(request, identity=ident)


def require_invite_member(
    request: Request, store: AuthStore = Depends(get_store)
) -> PolicyContext:
    ctx = require_authenticated_user(request, store)
    require_invite_only(user=ctx.user)
    return ctx


def require_request_allowed(request: Request) -> PolicyContext:
    require_remote_access(request=request)
    return PolicyContext.from_request(request)
