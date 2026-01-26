from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request

    from dubbing_pipeline.api.models import User
    from dubbing_pipeline.jobs.models import Job


def require_invite_only(*, request: Request, user: User | None = None) -> None:
    """
    Placeholder for invite-only enforcement.
    """
    raise NotImplementedError


def require_can_view_job(*, user: User, job: Job) -> None:
    """
    Placeholder for job visibility/ownership enforcement.
    """
    raise NotImplementedError


def require_quota(*, user: User, action: str, bytes: int | None = None) -> None:
    """
    Placeholder for quota enforcement (uploads, submissions, storage).
    """
    raise NotImplementedError


def require_remote_access(*, request: Request) -> None:
    """
    Placeholder for remote-access enforcement hook.
    """
    raise NotImplementedError
