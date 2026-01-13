from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from anime_v2.api.models import Role
from anime_v2.config import get_settings
from anime_v2.jobs.models import Job, JobState
from anime_v2.ops import audit
from anime_v2.utils.log import logger


@dataclass(frozen=True, slots=True)
class PolicyResult:
    ok: bool
    status_code: int
    detail: str
    effective_mode: str
    effective_device: str
    reasons: list[str]
    counts: dict[str, int]


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
        from anime_v2.jobs.limits import _same_utc_day  # type: ignore

        same_day = _same_utc_day
    except Exception:
        same_day = None

    now_iso = ""
    try:
        from anime_v2.jobs.models import now_utc

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


def evaluate_submission(
    *,
    jobs: list[Job],
    user_id: str,
    user_role: Role,
    requested_mode: str,
    requested_device: str,
    job_id: str | None = None,
) -> PolicyResult:
    """
    Evaluate submission policy for a new job.

    No side effects except best-effort audit logging.
    """
    s = get_settings()
    mode = str(requested_mode or "medium").strip().lower()
    device = str(requested_device or "auto").strip().lower()

    counts = _count_user_jobs(jobs, user_id=str(user_id))
    running = int(counts["running"])
    queued = int(counts["queued"])
    inflight = running + queued

    max_active = max(0, int(getattr(s, "max_active_jobs_per_user", 1)))
    max_queued = max(0, int(getattr(s, "max_queued_jobs_per_user", 5)))
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
    if daily_cap > 0 and int(counts["today"]) >= daily_cap and user_role != Role.admin:
        reasons.append("daily_job_cap")
        _audit_policy(
            "policy.job_rejected",
            user_id=user_id,
            job_id=job_id,
            meta={
                "reason": "daily_job_cap",
                "daily_cap": daily_cap,
                "today": int(counts["today"]),
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

    # Per-user inflight cap: safe default protects server from spam even if jobs haven't started yet.
    inflight_limit = max_active + max_queued
    if user_role != Role.admin and inflight_limit > 0 and inflight >= inflight_limit:
        reasons.append("user_inflight_cap")
        _audit_policy(
            "policy.job_rejected",
            user_id=user_id,
            job_id=job_id,
            meta={
                "reason": "user_inflight_cap",
                "inflight": inflight,
                "running": running,
                "queued": queued,
                "max_active": max_active,
                "max_queued": max_queued,
                "requested_mode": requested_mode,
            },
        )
        return PolicyResult(
            ok=False,
            status_code=429,
            detail=f"Too many in-flight jobs (running={running}, queued={queued}, limit={inflight_limit})",
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
            "max_active": max_active,
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

