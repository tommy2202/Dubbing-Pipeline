from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from dubbing_pipeline.api.models import Role, User
from dubbing_pipeline.jobs.models import Job, normalize_visibility


def is_admin(user: User) -> bool:
    role = getattr(user.role, "value", user.role)
    return str(role) == str(Role.admin.value)


def _normalize_vis(raw: Any) -> str:
    try:
        return normalize_visibility(str(raw or "")).value
    except Exception:
        # Fail-safe: treat unknown visibility as private.
        return "private"


def _can_view_by_visibility(
    *, user: User, owner_id: str | None, visibility: Any, allow_shared_read: bool
) -> bool:
    if is_admin(user) or str(owner_id or "") == str(user.id):
        return True
    if allow_shared_read:
        return _normalize_vis(visibility) == "shared"
    return False


def can_view_job(*, user: User, job: Job, allow_shared_read: bool = False) -> bool:
    return _can_view_by_visibility(
        user=user,
        owner_id=str(getattr(job, "owner_id", "") or ""),
        visibility=getattr(job, "visibility", None),
        allow_shared_read=allow_shared_read,
    )


def require_can_view_job(*, user: User, job: Job, allow_shared_read: bool = False) -> None:
    if not can_view_job(user=user, job=job, allow_shared_read=allow_shared_read):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def can_view_artifact(
    *, user: User, artifact: Any, job: Job, allow_shared_read: bool = False
) -> bool:
    _ = artifact
    return can_view_job(user=user, job=job, allow_shared_read=allow_shared_read)


def require_can_view_artifact(
    *, user: User, artifact: Any, job: Job, allow_shared_read: bool = False
) -> None:
    if not can_view_artifact(user=user, artifact=artifact, job=job, allow_shared_read=allow_shared_read):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def can_view_library_item(
    *, user: User, item: dict[str, Any], allow_shared_read: bool = False
) -> bool:
    return _can_view_by_visibility(
        user=user,
        owner_id=str(item.get("owner_user_id") or ""),
        visibility=item.get("visibility"),
        allow_shared_read=allow_shared_read,
    )


def require_can_view_library_item(
    *, user: User, item: dict[str, Any], allow_shared_read: bool = False
) -> None:
    if not can_view_library_item(user=user, item=item, allow_shared_read=allow_shared_read):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
