from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dubbing_pipeline.api.models import Role
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState
from dubbing_pipeline.ops import audit
from dubbing_pipeline.utils.log import logger


@dataclass(frozen=True, slots=True)
class PolicyResult:
    ok: bool
    status_code: int
    detail: str
    effective_mode: str
    effective_device: str
    reasons: list[str]
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class DispatchDecision:
    """
    Dispatch-time safety net decision.

    This must use the same canonical policy logic as submission-time enforcement, but
    it returns a simpler shape (no HTTP codes).
    """

    ok: bool
    reasons: list[str]
    # Optional retry hint for queue backends (seconds).
    retry_after_s: float | None = None


def _gpu_available() -> bool:
    try:
        import torch  # type: ignore

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _count_user_jobs(jobs: list[Job], *, user_id: str) -> dict[str, int]:
    running = 0
    queued = 0
    total_today = 0
    # today: reuse created_at day logic from jobs.limits if possible
    try:
        from dubbing_pipeline.jobs.limits import _same_utc_day  # type: ignore

        same_day = _same_utc_day
    except Exception:
        same_day = None

    now_iso = ""
    try:
        from dubbing_pipeline.jobs.models import now_utc

        now_iso = now_utc()
    except Exception:
        now_iso = ""

    for j in jobs:
        if str(j.owner_id or "") != str(user_id):
            continue
        if j.state == JobState.RUNNING:
            running += 1
        if j.state == JobState.QUEUED:
            queued += 1
        if same_day and now_iso:
            try:
                if same_day(str(j.created_at or ""), now_iso):
                    total_today += 1
            except Exception:
                pass
    return {"running": running, "queued": queued, "today": total_today}


def _resolve_limits_for_user(*, user_role: Role, user_quota: dict[str, int] | None = None) -> tuple[int, int]:
    """
    Resolve per-user max_running and max_queued.

    - Defaults come from PublicConfig.
    - Admins can still be constrained by quotas if explicitly set, but by default bypass caps.
    """
    s = get_settings()
    max_active = max(0, int(getattr(s, "max_active_jobs_per_user", 1)))
    max_queued = max(0, int(getattr(s, "max_queued_jobs_per_user", 5)))
    if user_role == Role.admin and not user_quota:
        # Admin default: allow (policy safety remains for high-mode admin-only and global caps).
        return max_active, max_queued
    if isinstance(user_quota, dict):
        if "max_running" in user_quota:
            with __import__("contextlib").suppress(Exception):
                max_active = max(0, int(user_quota["max_running"]))
        if "max_queued" in user_quota:
            with __import__("contextlib").suppress(Exception):
                max_queued = max(0, int(user_quota["max_queued"]))
    return max_active, max_queued


def evaluate_dispatch(
    *,
    user_id: str,
    user_role: Role,
    requested_mode: str,
    # Current counters from the queue backend (Redis in L2, local scan in fallback).
    running: int,
    queued: int,
    # Global counters (Redis in L2). Only required for enforcing global high-mode cap.
    global_high_running: int | None = None,
    # Optional per-user quota overrides (admin-controlled).
    user_quota: dict[str, int] | None = None,
    job_id: str | None = None,
) -> DispatchDecision:
    """
    Dispatch-time safety net.

    Enforced even if submission-time checks were skipped/stale.
    """
    s = get_settings()
    mode = str(requested_mode or "medium").strip().lower()
    high_admin_only = bool(getattr(s, "high_mode_admin_only", True))

    reasons: list[str] = []
    if mode == "high" and high_admin_only and user_role != Role.admin:
        reasons.append("high_mode_admin_only")
        return DispatchDecision(ok=False, reasons=reasons, retry_after_s=60.0)

    max_active, max_queued = _resolve_limits_for_user(user_role=user_role, user_quota=user_quota)
    # Safety net: do not start running if user already at max_active.
    if user_role != Role.admin and max_active > 0 and int(running) >= int(max_active):
        reasons.append("user_running_cap")
        return DispatchDecision(ok=False, reasons=reasons, retry_after_s=5.0)

    # Global high-mode running cap (cross-instance, Redis-backed).
    max_high_running_global = max(0, int(getattr(s, "max_high_running_global", 1)))
    if mode == "high" and max_high_running_global > 0:
        cur = int(global_high_running or 0)
        if cur >= max_high_running_global:
            reasons.append("global_high_running_cap")
            return DispatchDecision(ok=False, reasons=reasons, retry_after_s=10.0)

    # OK to dispatch.
    return DispatchDecision(ok=True, reasons=reasons)


def evaluate_submission(
    *,
    jobs: list[Job],
    user_id: str,
    user_role: Role,
    requested_mode: str,
    requested_device: str,
    job_id: str | None = None,
    # Optional: override counts from a queue backend (Redis L2) to avoid stale per-process counts.
    counts_override: dict[str, int] | None = None,
    # Optional: per-user quota overrides (admin-controlled; queue backend stores these).
    user_quota: dict[str, int] | None = None,
) -> PolicyResult:
    """
    Evaluate submission policy for a new job.

    No side effects except best-effort audit logging.
    """
    s = get_settings()
    mode = str(requested_mode or "medium").strip().lower()
    device = str(requested_device or "auto").strip().lower()

    if isinstance(counts_override, dict):
        running = int(counts_override.get("running") or 0)
        queued = int(counts_override.get("queued") or 0)
        today = int(counts_override.get("today") or 0)
    else:
        counts = _count_user_jobs(jobs, user_id=str(user_id))
        running = int(counts["running"])
        queued = int(counts["queued"])
        today = int(counts["today"])
    inflight = running + queued

    max_active, max_queued = _resolve_limits_for_user(user_role=user_role, user_quota=user_quota)
    daily_cap = max(0, int(getattr(s, "daily_job_cap", 0)))
    high_admin_only = bool(getattr(s, "high_mode_admin_only", True))

    reasons: list[str] = []

    # High mode restricted to admin by default.
    if mode == "high" and high_admin_only and user_role != Role.admin:
        reasons.append("high_mode_admin_only")
        _audit_policy(
            "policy.job_rejected",
            user_id=user_id,
            job_id=job_id,
            meta={
                "reason": "high_mode_admin_only",
                "requested_mode": requested_mode,
                "requested_device": requested_device,
                "running": running,
                "queued": queued,
            },
        )
        return PolicyResult(
            ok=False,
            status_code=403,
            detail="high mode is restricted to admin",
            effective_mode=mode,
            effective_device=device,
            reasons=reasons,
            counts={"running": running, "queued": queued, "inflight": inflight},
        )

    # GPU fallback: if CUDA is requested but unavailable, downgrade to CPU.
    has_gpu = _gpu_available()
    if device == "cuda" and not has_gpu:
        reasons.append("gpu_unavailable_device_downgrade")
        device = "cpu"
        # If user requested high mode without GPU, downgrade to medium.
        if mode == "high":
            reasons.append("gpu_unavailable_mode_downgrade")
            mode = "medium"

    # Per-user daily job cap (optional).
    if daily_cap > 0 and int(today) >= daily_cap and user_role != Role.admin:
        reasons.append("daily_job_cap")
        _audit_policy(
            "policy.job_rejected",
            user_id=user_id,
            job_id=job_id,
            meta={
                "reason": "daily_job_cap",
                "daily_cap": daily_cap,
                "today": int(today),
                "requested_mode": requested_mode,
            },
        )
        return PolicyResult(
            ok=False,
            status_code=429,
            detail=f"Daily job cap exceeded (limit={daily_cap})",
            effective_mode=mode,
            effective_device=device,
            reasons=reasons,
            counts={"running": running, "queued": queued, "inflight": inflight},
        )

    # Per-user queued cap: safe default for unknown concurrency.
    # Note: running cap is enforced at dispatch time (safety net) so users can queue while one runs.
    if user_role != Role.admin and max_queued > 0 and queued >= max_queued:
        reasons.append("user_queued_cap")
        _audit_policy(
            "policy.job_rejected",
            user_id=user_id,
            job_id=job_id,
            meta={
                "reason": "user_queued_cap",
                "running": running,
                "queued": queued,
                "max_running": max_active,
                "max_queued": max_queued,
                "requested_mode": requested_mode,
            },
        )
        return PolicyResult(
            ok=False,
            status_code=429,
            detail=f"Too many queued jobs (queued={queued}, limit={max_queued})",
            effective_mode=mode,
            effective_device=device,
            reasons=reasons,
            counts={"running": running, "queued": queued, "inflight": inflight},
        )

    # OK: accept job; it may still be queued by scheduler/global caps.
    _audit_policy(
        "policy.job_accepted",
        user_id=user_id,
        job_id=job_id,
        meta={
            "running": running,
            "queued": queued,
            "max_running": max_active,
            "max_queued": max_queued,
            "requested_mode": requested_mode,
            "requested_device": requested_device,
            "effective_mode": mode,
            "effective_device": device,
            "reasons": reasons,
        },
    )
    return PolicyResult(
        ok=True,
        status_code=200,
        detail="ok",
        effective_mode=mode,
        effective_device=device,
        reasons=reasons,
        counts={"running": running, "queued": queued, "inflight": inflight},
    )


def _audit_policy(event: str, *, user_id: str, job_id: str | None, meta: dict[str, Any]) -> None:
    # Best-effort; must never throw or block job submission.
    try:
        audit.emit(event, user_id=str(user_id), job_id=str(job_id) if job_id else None, meta=dict(meta))
    except Exception:
        pass
    # Always emit a normal log line for debugging.
    try:
        logger.info(event, user_id=str(user_id), job_id=str(job_id) if job_id else None, **dict(meta))
    except Exception:
        pass

