from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class QueueStatus:
    mode: str  # "redis" | "fallback"
    redis_configured: bool
    redis_ok: bool
    detail: str
    banner: str | None = None


class QueueBackend(Protocol):
    """
    Single canonical queue interface.

    - SQLite remains the source of truth for job metadata/state.
    - Redis (when enabled) is the source of truth for queue state + locks + counters.
    - Fallback mode uses the existing in-proc scheduler/queue (Level 1).
    """

    def status(self) -> QueueStatus: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def submit_job(
        self,
        *,
        job_id: str,
        user_id: str,
        mode: str,
        device: str,
        priority: int = 100,
        meta: dict[str, Any] | None = None,
    ) -> None: ...

    async def cancel_job(self, *, job_id: str, user_id: str | None = None) -> None: ...

    async def user_counts(self, *, user_id: str) -> dict[str, int]: ...
    async def user_quota(self, *, user_id: str) -> dict[str, int] | None: ...

    async def admin_snapshot(self, *, limit: int = 200) -> dict[str, Any]: ...
    async def admin_set_priority(self, *, job_id: str, priority: int) -> bool: ...
    async def admin_set_user_quotas(
        self, *, user_id: str, max_running: int | None, max_queued: int | None
    ) -> dict[str, int]: ...

    async def global_counts(self) -> dict[str, int]: ...

    async def before_job_run(self, *, job_id: str, user_id: str | None) -> bool: ...
    async def after_job_run(
        self,
        *,
        job_id: str,
        user_id: str | None,
        final_state: str,
        ok: bool,
        error: str | None = None,
    ) -> None: ...

