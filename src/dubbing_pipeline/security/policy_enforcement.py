from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException, status

from dubbing_pipeline.api import invites as _invites
from dubbing_pipeline.api import remote_access as _remote_access
from dubbing_pipeline.security import quotas as _quotas
from dubbing_pipeline.security import visibility as _visibility

if TYPE_CHECKING:
    from fastapi import Request

    from dubbing_pipeline.api.models import User
    from dubbing_pipeline.jobs.models import Job


def require_invite_only(*, request: Request, user: User | None = None) -> None:
    """
    Invite-only enforcement is handled by disabled registration routes and invite redemption.

    This shim intentionally has no side effects beyond referencing the invites module so
    it is safe to import and wire later without reintroducing placeholders.
    """
    _ = user
    token = request.headers.get("x-invite-token") or ""
    _invites.invite_token_hash(str(token))


def require_can_view_job(
    *, user: User, job: Job, allow_shared_read: bool = False
) -> None:
    return _visibility.require_can_view_job(
        user=user, job=job, allow_shared_read=allow_shared_read
    )


async def require_quota(
    *,
    request: Request | None = None,
    user: User,
    action: str,
    bytes: int | None = None,
) -> None:
    """
    Thin async wrapper for quota enforcement when upload bytes are known.

    For other quota checks, call QuotaEnforcer directly.
    """
    if request is None or bytes is None:
        return
    enforcer = _quotas.QuotaEnforcer.from_request(request=request, user=user)
    await enforcer.require_upload_bytes(total_bytes=int(bytes), action=str(action))


def require_remote_access(*, request: Request) -> None:
    decision = _remote_access.decide_remote_access(request)
    if decision.allowed:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"detail": "Forbidden", "reason": decision.reason, "mode": decision.mode},
    )
