from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from fastapi import Depends, HTTPException, Request, status

from dubbing_pipeline.api import remote_access
from dubbing_pipeline.api.deps import Identity, current_identity, get_store, require_role
from dubbing_pipeline.api.middleware import audit_event
from dubbing_pipeline.api.models import AuthStore, Role, User
from dubbing_pipeline.config import get_settings
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


def _empty_policy_context() -> PolicyContext:
    return PolicyContext(
        user=None,
        roles=[],
        request_id=None,
        client_ip=None,
        access_mode=None,
        method=None,
        path=None,
    )


def _context_with_user(ctx: PolicyContext, user: User | None) -> PolicyContext:
    role = None
    if user is not None:
        role = getattr(user.role, "value", user.role)
    roles = [str(role)] if role is not None else []
    return replace(ctx, user=user, roles=roles)


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


def require_can_view_artifact(
    *, user: User, artifact, job, allow_shared_read: bool = False
) -> None:
    visibility.require_can_view_artifact(
        user=user, artifact=artifact, job=job, allow_shared_read=allow_shared_read
    )


def require_can_view_library_item(
    *, user: User, item: dict[str, Any], allow_shared_read: bool = False
) -> None:
    visibility.require_can_view_library_item(
        user=user, item=item, allow_shared_read=allow_shared_read
    )


def _share_allowed(user: User) -> bool:
    if user.role == Role.admin:
        return True
    s = get_settings()
    return bool(getattr(s, "allow_shared_library", True))


def require_share_allowed(*, user: User, visibility_value: str) -> None:
    vis = str(visibility_value or "").strip().lower()
    if vis in {"shared", "public"} and not _share_allowed(user):
        raise PolicyError.forbidden("Sharing is disabled")


async def require_quota(
    *, request: Request | None, user: User, action: str, bytes: int = 0
) -> None:
    if request is None or int(bytes) <= 0:
        return
    enforcer = quotas.QuotaEnforcer.from_request(request=request, user=user)
    await enforcer.require_upload_bytes(total_bytes=int(bytes), action=str(action))


async def quota_snapshot(*, request: Request, user: User) -> quotas.QuotaSnapshot:
    enforcer = quotas.QuotaEnforcer.from_request(request=request, user=user)
    return await enforcer.snapshot()


async def require_upload_progress(
    *, request: Request, user: User, written_bytes: int, action: str
) -> None:
    enforcer = quotas.QuotaEnforcer.from_request(request=request, user=user)
    await enforcer.require_upload_progress(
        written_bytes=int(written_bytes), action=str(action)
    )


async def reserve_storage_bytes(
    *, request: Request, user: User, bytes_count: int, action: str
) -> quotas.StorageReservation:
    enforcer = quotas.QuotaEnforcer.from_request(request=request, user=user)
    return await enforcer.reserve_storage_bytes(
        bytes_count=int(bytes_count), action=str(action)
    )


async def reserve_daily_jobs(
    *, request: Request, user: User, count: int, action: str
) -> quotas.JobReservation:
    enforcer = quotas.QuotaEnforcer.from_request(request=request, user=user)
    return await enforcer.reserve_daily_jobs(count=int(count), action=str(action))


async def apply_submission_policy(
    *,
    request: Request,
    user: User,
    requested_mode: str,
    requested_device: str,
    job_id: str | None = None,
):
    enforcer = quotas.QuotaEnforcer.from_request(request=request, user=user)
    return await enforcer.apply_submission_policy(
        requested_mode=str(requested_mode),
        requested_device=str(requested_device),
        job_id=str(job_id) if job_id else None,
    )


async def require_quota_for_upload(
    *, request: Request | None, user: User, bytes: int, action: str = "upload"
) -> None:
    await require_quota(request=request, user=user, action=str(action), bytes=int(bytes))


async def require_quota_for_submit(
    *,
    request: Request,
    user: User,
    count: int = 1,
    requested_mode: str = "medium",
    requested_device: str = "auto",
    job_id: str | None = None,
    action: str = "jobs.submit",
) -> quotas.JobReservation:
    await require_concurrent_jobs(request=request, user=user, action=str(action))
    return await reserve_submit_jobs(
        request=request,
        user=user,
        count=int(count),
        requested_mode=str(requested_mode),
        requested_device=str(requested_device),
        job_id=str(job_id) if job_id else None,
        action=str(action),
    )


async def require_concurrent_jobs(*, request: Request, user: User, action: str) -> None:
    enforcer = quotas.QuotaEnforcer.from_request(request=request, user=user)
    await enforcer.require_concurrent_jobs(action=str(action))


async def reserve_submit_jobs(
    *,
    request: Request,
    user: User,
    count: int,
    requested_mode: str,
    requested_device: str,
    job_id: str | None,
    action: str,
) -> quotas.JobReservation:
    enforcer = quotas.QuotaEnforcer.from_request(request=request, user=user)
    return await enforcer.reserve_submit_jobs(
        count=int(count),
        requested_mode=str(requested_mode),
        requested_device=str(requested_device),
        job_id=str(job_id) if job_id else None,
        action=str(action),
    )


async def require_processing_minutes(
    *, request: Request, user: User, duration_s: float, action: str
) -> None:
    enforcer = quotas.QuotaEnforcer.from_request(request=request, user=user)
    await enforcer.require_processing_minutes(duration_s=float(duration_s), action=str(action))


def _resolve_identity(request: Request, store: AuthStore) -> Identity:
    ident = current_identity(request, store)
    try:
        request.state.identity = ident
    except Exception:
        pass
    return ident


def _identity_from_request(request: Request, store: AuthStore | None) -> Identity | None:
    ident = getattr(getattr(request, "state", None), "identity", None)
    if isinstance(ident, Identity):
        return ident
    if isinstance(store, AuthStore):
        return _resolve_identity(request, store)
    return None


def dep_user(request: Request, store: AuthStore = Depends(get_store)) -> User:
    ident = _resolve_identity(request, store)
    return ident.user


def require_admin(ident: Identity = Depends(require_role(Role.admin))) -> Identity:
    return ident


def require_authenticated_user(
    request: Request, store: AuthStore = Depends(get_store)
) -> PolicyContext:
    ident = _resolve_identity(request, store)
    return PolicyContext.from_request(request, identity=ident)


def require_invite_member(
    request: Request | None = None,
    user: User = Depends(dep_user),
    store: AuthStore = Depends(get_store),
) -> PolicyContext:
    require_invite_only(user=user)
    if request is None:
        return _context_with_user(_empty_policy_context(), user)
    ident = _identity_from_request(request, store)
    if isinstance(ident, Identity):
        return PolicyContext.from_request(request, identity=ident)
    ctx = PolicyContext.from_request(request)
    return _context_with_user(ctx, user)


def dep_invite_only(user: User = Depends(dep_user)) -> User:
    require_invite_member(user=user)
    return user


def dep_request_allowed(request: Request) -> Request:
    require_remote_access(request=request)
    return request


def require_request_allowed(request: Request) -> PolicyContext:
    dep_request_allowed(request)
    return PolicyContext.from_request(request)
