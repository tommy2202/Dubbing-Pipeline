from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Awaitable, Callable

from fastapi import Request

from dubbing_pipeline.api.models import Role, User
from dubbing_pipeline.jobs.limits import get_limits, resolve_user_quotas, used_minutes_today
from dubbing_pipeline.jobs.models import now_utc
from dubbing_pipeline.jobs.policy import evaluate_submission
from dubbing_pipeline.ops import audit
from dubbing_pipeline.utils.log import logger


class QuotaStatus(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class QuotaExceededError(Exception):
    code: str
    limit: int | None
    remaining: int | None
    reset_seconds: int
    detail: str
    status_code: int = 429


@dataclass(frozen=True, slots=True)
class QuotaDecision:
    status: QuotaStatus
    reason: str
    limit: int | None = None
    current: int | None = None


@dataclass
class JobReservation:
    count: int
    user_id: str | None = None
    backend: str | None = None
    effective_mode: str | None = None
    effective_device: str | None = None
    release_cb: Callable[[int], Awaitable[None]] | None = None
    released: bool = False

    async def release(self, *, count: int | None = None) -> None:
        if self.released:
            return
        release_count = self.count if count is None else max(0, int(count))
        if self.release_cb is None or release_count <= 0:
            self.released = True
            return
        try:
            await self.release_cb(int(release_count))
        except Exception as ex:
            logger.warning(
                "quota_job_reservation_release_failed",
                user_id=str(self.user_id or ""),
                count=int(release_count),
                backend=str(self.backend or ""),
                error=str(ex),
            )
        finally:
            self.released = True


@dataclass
class StorageReservation:
    bytes_count: int
    user_id: str | None = None
    backend: str | None = None
    release_cb: Callable[[int], Awaitable[None]] | None = None
    released: bool = False

    async def release(self, *, bytes_count: int | None = None) -> None:
        if self.released:
            return
        release_bytes = self.bytes_count if bytes_count is None else max(0, int(bytes_count))
        if self.release_cb is None or release_bytes <= 0:
            self.released = True
            return
        try:
            await self.release_cb(int(release_bytes))
        except Exception as ex:
            logger.warning(
                "quota_storage_reservation_release_failed",
                user_id=str(self.user_id or ""),
                bytes=int(release_bytes),
                backend=str(self.backend or ""),
                error=str(ex),
            )
        finally:
            self.released = True


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    max_upload_bytes: int
    max_storage_bytes: int
    jobs_per_day: int
    max_concurrent_jobs: int


_LOCAL_LOCK = asyncio.Lock()
_LOCAL_DAILY_RESERVATIONS: dict[tuple[str, str], int] = {}
_LOCAL_PENDING_STORAGE: dict[str, int] = {}
_REDIS_CLIENT = None
_REDIS_LOCK = asyncio.Lock()


def _utc_day_key(ts: float | None = None) -> str:
    now = datetime.now(tz=timezone.utc) if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc)
    return now.strftime("%Y%m%d")


def _seconds_until_utc_day_rollover(ts: float | None = None) -> int:
    now = datetime.now(tz=timezone.utc) if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc)
    next_day = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
    return max(60, int((next_day - now).total_seconds()))


def _log_quota_denied(*, user: User, action: str, reason: str, limit: int | None, current: int | None):
    logger.warning(
        "quota_denied",
        user_id=str(user.id),
        action=str(action),
        reason=str(reason),
        limit=int(limit) if limit is not None else None,
        current=int(current) if current is not None else None,
    )
    with __import__("contextlib").suppress(Exception):
        audit.emit(
            "quota.denied",
            user_id=str(user.id),
            meta={
                "action": str(action),
                "code": str(reason),
            },
        )


def _remaining(limit: int | None, current: int | None) -> int | None:
    if limit is None or current is None:
        return None
    return max(0, int(limit) - int(current))


def _quota_reset_seconds(reason: str) -> int:
    if reason in {"jobs_per_day_limit", "daily_processing_minutes"}:
        return int(_seconds_until_utc_day_rollover())
    return 0


def _raise_quota(
    *,
    user: User,
    action: str,
    reason: str,
    limit: int | None,
    current: int | None,
    detail: str | None = None,
) -> None:
    _log_quota_denied(user=user, action=action, reason=reason, limit=limit, current=current)
    raise QuotaExceededError(
        code=str(reason),
        limit=int(limit) if limit is not None else None,
        remaining=_remaining(limit, current),
        reset_seconds=_quota_reset_seconds(str(reason)),
        detail=str(detail or reason),
    )


def raise_quota_exceeded(
    *,
    user: User,
    action: str,
    code: str,
    limit: int | None = None,
    current: int | None = None,
    detail: str | None = None,
) -> None:
    _raise_quota(
        user=user,
        action=action,
        reason=code,
        limit=limit,
        current=current,
        detail=detail,
    )


def _admin_bypass(user: User, user_quota: dict[str, int] | None) -> bool:
    if user.role != Role.admin:
        return False
    if not user_quota:
        return True
    return False


async def _redis():
    global _REDIS_CLIENT
    async with _REDIS_LOCK:
        if _REDIS_CLIENT is not None:
            return _REDIS_CLIENT
        try:
            import redis.asyncio as redis  # type: ignore

            from dubbing_pipeline.config import get_settings

            url = str(getattr(get_settings(), "redis_url", "") or "").strip()
            if not url:
                return None
            _REDIS_CLIENT = redis.Redis.from_url(url, decode_responses=True)
            return _REDIS_CLIENT
        except Exception as ex:
            logger.warning("quota.redis_init_failed", error=str(ex))
            _REDIS_CLIENT = None
            return None


def _redis_prefix() -> str:
    from dubbing_pipeline.config import get_settings

    s = get_settings()
    prefix = str(getattr(s, "redis_queue_prefix", "dp") or "dp").strip().strip(":") or "dp"
    return prefix


async def _reserve_daily_redis(
    *, user_id: str, count: int, limit: int
) -> tuple[bool, int] | None:
    r = await _redis()
    if r is None:
        return None
    day = _utc_day_key()
    ttl = _seconds_until_utc_day_rollover()
    key = f"{_redis_prefix()}:quota:daily:{day}:{user_id}"
    lua = """
    local key = KEYS[1]
    local limit = tonumber(ARGV[1])
    local count = tonumber(ARGV[2])
    local ttl = tonumber(ARGV[3])
    local cur = tonumber(redis.call("GET", key) or "0")
    if limit <= 0 then
      return {1, cur}
    end
    if (cur + count) > limit then
      return {0, cur}
    end
    cur = redis.call("INCRBY", key, count)
    if cur == count then
      redis.call("EXPIRE", key, ttl)
    end
    return {1, cur}
    """
    try:
        ok, cur = await r.eval(lua, 1, key, str(int(limit)), str(int(count)), str(int(ttl)))
        return bool(ok), int(cur or 0)
    except Exception:
        return None


async def _release_daily_redis(*, user_id: str, count: int) -> None:
    r = await _redis()
    if r is None:
        return
    day = _utc_day_key()
    key = f"{_redis_prefix()}:quota:daily:{day}:{user_id}"
    lua = """
    local key = KEYS[1]
    local count = tonumber(ARGV[1])
    local cur = tonumber(redis.call("GET", key) or "0")
    if cur <= 0 then
      return 0
    end
    local next = cur - count
    if next < 0 then
      next = 0
    end
    redis.call("SET", key, tostring(next))
    return next
    """
    with __import__("contextlib").suppress(Exception):
        await r.eval(lua, 1, key, str(int(count)))


async def _reserve_daily_local(*, store, user_id: str, count: int, limit: int) -> tuple[bool, int]:
    async with _LOCAL_LOCK:
        day = _utc_day_key()
        key = (day, user_id)
        if limit <= 0:
            return True, 0
        # Base count from store (persistent) + local reservations for this day.
        try:
            jobs = store.list(limit=2000)
        except Exception:
            jobs = []
        today = 0
        try:
            from dubbing_pipeline.jobs.limits import _same_utc_day  # type: ignore
            from dubbing_pipeline.jobs.models import now_utc

            now_iso = now_utc()
            for j in jobs:
                if str(getattr(j, "owner_id", "") or "") != str(user_id):
                    continue
                if _same_utc_day(str(getattr(j, "created_at", "") or ""), now_iso):
                    today += 1
        except Exception:
            today = len([j for j in jobs if str(getattr(j, "owner_id", "") or "") == str(user_id)])
        reserved = int(_LOCAL_DAILY_RESERVATIONS.get(key, 0))
        current = int(today + reserved)
        if (current + count) > limit:
            return False, current
        _LOCAL_DAILY_RESERVATIONS[key] = reserved + int(count)
        return True, current + count


async def _release_daily_local(*, user_id: str, count: int) -> None:
    async with _LOCAL_LOCK:
        day = _utc_day_key()
        key = (day, user_id)
        if key not in _LOCAL_DAILY_RESERVATIONS:
            return
        cur = int(_LOCAL_DAILY_RESERVATIONS.get(key, 0))
        cur = max(0, cur - int(count))
        if cur <= 0:
            _LOCAL_DAILY_RESERVATIONS.pop(key, None)
        else:
            _LOCAL_DAILY_RESERVATIONS[key] = cur


class QuotaEnforcer:
    def __init__(self, *, request: Request, user: User):
        self._request = request
        self._user = user
        self._store = getattr(request.app.state, "job_store", None)
        self._queue_backend = getattr(request.app.state, "queue_backend", None)
        self._quota_snapshot: QuotaSnapshot | None = None
        self._quota_overrides: dict[str, int] | None = None

    @classmethod
    def from_request(cls, *, request: Request, user: User) -> "QuotaEnforcer":
        return cls(request=request, user=user)

    async def _load_overrides(self) -> dict[str, int]:
        if self._quota_overrides is not None:
            return dict(self._quota_overrides)
        overrides: dict[str, int] = {}
        if self._store is not None:
            try:
                rec = self._store.get_user_quota(str(self._user.id))
                if isinstance(rec, dict):
                    overrides.update({k: int(v) for k, v in rec.items() if v is not None})
            except Exception:
                pass
        if self._queue_backend is not None:
            with __import__("contextlib").suppress(Exception):
                qb_quota = await self._queue_backend.user_quota(user_id=str(self._user.id))
                if isinstance(qb_quota, dict):
                    overrides.update({k: int(v) for k, v in qb_quota.items() if v is not None})
        self._quota_overrides = dict(overrides)
        return dict(overrides)

    async def snapshot(self) -> QuotaSnapshot:
        if self._quota_snapshot is not None:
            return self._quota_snapshot
        overrides = await self._load_overrides()
        quotas = resolve_user_quotas(overrides=overrides)
        snap = QuotaSnapshot(
            max_upload_bytes=int(quotas.max_upload_bytes or 0),
            max_storage_bytes=int(quotas.max_storage_bytes_per_user or 0),
            jobs_per_day=int(quotas.jobs_per_day_per_user or 0),
            max_concurrent_jobs=int(quotas.max_concurrent_jobs_per_user or 0),
        )
        self._quota_snapshot = snap
        return snap

    async def check_upload_bytes(self, *, total_bytes: int, action: str) -> QuotaDecision:
        snap = await self.snapshot()
        if snap.max_upload_bytes > 0 and int(total_bytes) > int(snap.max_upload_bytes):
            return QuotaDecision(
                status=QuotaStatus.FAIL,
                reason="upload_bytes_limit",
                limit=int(snap.max_upload_bytes),
                current=int(total_bytes),
            )
        if snap.max_storage_bytes > 0 and self._store is not None:
            used = int(self._store.get_user_storage_bytes(str(self._user.id)) or 0)
            pending = int(_LOCAL_PENDING_STORAGE.get(str(self._user.id), 0))
            if (used + pending + int(total_bytes)) > int(snap.max_storage_bytes):
                return QuotaDecision(
                    status=QuotaStatus.FAIL,
                    reason="storage_bytes_limit",
                    limit=int(snap.max_storage_bytes),
                    current=int(used + pending + int(total_bytes)),
                )
        return QuotaDecision(status=QuotaStatus.PASS, reason="ok")

    async def require_upload_bytes(self, *, total_bytes: int, action: str) -> None:
        decision = await self.check_upload_bytes(total_bytes=total_bytes, action=action)
        if decision.status == QuotaStatus.FAIL:
            _raise_quota(
                user=self._user,
                action=action,
                reason=decision.reason,
                limit=decision.limit,
                current=decision.current,
            )

    async def require_upload_progress(self, *, written_bytes: int, action: str) -> None:
        snap = await self.snapshot()
        if snap.max_upload_bytes > 0 and int(written_bytes) > int(snap.max_upload_bytes):
            _raise_quota(
                user=self._user,
                action=action,
                reason="upload_bytes_limit",
                limit=int(snap.max_upload_bytes),
                current=int(written_bytes),
            )

    async def check_concurrent_jobs(self, *, action: str) -> QuotaDecision:
        overrides = await self._load_overrides()
        if _admin_bypass(self._user, overrides):
            return QuotaDecision(status=QuotaStatus.PASS, reason="admin_bypass")
        snap = await self.snapshot()
        if snap.max_concurrent_jobs <= 0:
            return QuotaDecision(status=QuotaStatus.PASS, reason="no_limit")
        counts = {"running": 0}
        if self._queue_backend is not None:
            with __import__("contextlib").suppress(Exception):
                counts = await self._queue_backend.user_counts(user_id=str(self._user.id))
        elif self._store is not None:
            with __import__("contextlib").suppress(Exception):
                jobs = self._store.list(limit=2000)
                running = 0
                for j in jobs:
                    if str(getattr(j, "owner_id", "") or "") != str(self._user.id):
                        continue
                    if str(getattr(getattr(j, "state", None), "value", "") or "") == "RUNNING":
                        running += 1
                counts["running"] = running
        running = int(counts.get("running") or 0)
        if running >= int(snap.max_concurrent_jobs):
            return QuotaDecision(
                status=QuotaStatus.FAIL,
                reason="concurrent_jobs_limit",
                limit=int(snap.max_concurrent_jobs),
                current=int(running),
            )
        return QuotaDecision(status=QuotaStatus.PASS, reason="ok")

    async def require_concurrent_jobs(self, *, action: str) -> None:
        decision = await self.check_concurrent_jobs(action=action)
        if decision.status == QuotaStatus.FAIL:
            _raise_quota(
                user=self._user,
                action=action,
                reason=decision.reason,
                limit=decision.limit,
                current=decision.current,
            )

    async def reserve_submit_jobs(
        self,
        *,
        count: int,
        requested_mode: str,
        requested_device: str,
        job_id: str | None,
        action: str,
    ) -> JobReservation:
        if self._store is None:
            return JobReservation(count=0, user_id=str(self._user.id), backend="none")
        overrides = await self._load_overrides()
        reservation = await self.reserve_daily_jobs(count=count, action=action)

        # Policy check: queued cap / high-mode enforcement, daily cap is handled above.
        counts_override = None
        if self._queue_backend is not None:
            with __import__("contextlib").suppress(Exception):
                counts_override = await self._queue_backend.user_counts(user_id=str(self._user.id))
        user_quota = dict(overrides or {})
        user_quota["jobs_per_day"] = 0
        try:
            pol = evaluate_submission(
                jobs=self._store.list(limit=1000),
                user_id=str(self._user.id),
                user_role=self._user.role,
                requested_mode=str(requested_mode or "medium"),
                requested_device=str(requested_device or "auto"),
                job_id=str(job_id) if job_id else None,
                counts_override=counts_override,
                user_quota=user_quota,
            )
        except Exception:
            await reservation.release()
            raise
        if not pol.ok:
            await reservation.release()
            if int(pol.status_code) == 429:
                _raise_quota(
                    user=self._user,
                    action=action,
                    reason="submission_policy_limit",
                    limit=None,
                    current=None,
                )
            raise HTTPException(status_code=int(pol.status_code), detail=str(pol.detail))

        reservation.effective_mode = str(pol.effective_mode or requested_mode)
        reservation.effective_device = str(pol.effective_device or requested_device)
        return reservation

    async def reserve_daily_jobs(self, *, count: int, action: str) -> JobReservation:
        overrides = await self._load_overrides()
        snap = await self.snapshot()
        reservation = JobReservation(count=0, user_id=str(self._user.id), backend="none")
        if _admin_bypass(self._user, overrides) or snap.jobs_per_day <= 0:
            return reservation
        redis_res = await _reserve_daily_redis(
            user_id=str(self._user.id), count=int(count), limit=int(snap.jobs_per_day)
        )
        if redis_res is not None:
            ok, current = redis_res
            if not ok:
                _raise_quota(
                    user=self._user,
                    action=action,
                    reason="jobs_per_day_limit",
                    limit=int(snap.jobs_per_day),
                    current=int(current),
                )
            return JobReservation(
                count=int(count),
                user_id=str(self._user.id),
                backend="redis",
                release_cb=lambda n: _release_daily_redis(user_id=str(self._user.id), count=int(n)),
            )
        ok, current = await _reserve_daily_local(
            store=self._store,
            user_id=str(self._user.id),
            count=int(count),
            limit=int(snap.jobs_per_day),
        )
        if not ok:
            _raise_quota(
                user=self._user,
                action=action,
                reason="jobs_per_day_limit",
                limit=int(snap.jobs_per_day),
                current=int(current),
            )
        return JobReservation(
            count=int(count),
            user_id=str(self._user.id),
            backend="local",
            release_cb=lambda n: _release_daily_local(user_id=str(self._user.id), count=int(n)),
        )

    async def apply_submission_policy(
        self, *, requested_mode: str, requested_device: str, job_id: str | None
    ):
        if self._store is None:
            return None
        overrides = await self._load_overrides()
        counts_override = None
        if self._queue_backend is not None:
            with __import__("contextlib").suppress(Exception):
                counts_override = await self._queue_backend.user_counts(user_id=str(self._user.id))
        user_quota = dict(overrides or {})
        user_quota["jobs_per_day"] = 0
        return evaluate_submission(
            jobs=self._store.list(limit=1000),
            user_id=str(self._user.id),
            user_role=self._user.role,
            requested_mode=str(requested_mode or "medium"),
            requested_device=str(requested_device or "auto"),
            job_id=str(job_id) if job_id else None,
            counts_override=counts_override,
            user_quota=user_quota,
        )

    async def require_processing_minutes(
        self, *, duration_s: float, action: str
    ) -> None:
        if self._store is None:
            return
        limits = get_limits()
        req_min = float(duration_s or 0.0) / 60.0
        if req_min <= 0:
            return
        jobs = self._store.list(limit=1000)
        used_min = used_minutes_today(jobs, user_id=self._user.id, now_iso=now_utc())
        if (used_min + req_min) > float(limits.daily_processing_minutes):
            _raise_quota(
                user=self._user,
                action=action,
                reason="daily_processing_minutes",
                limit=int(limits.daily_processing_minutes),
                current=int(used_min + req_min),
            )

    async def check_storage_bytes(self, *, bytes_count: int, action: str) -> QuotaDecision:
        _ = action
        snap = await self.snapshot()
        if snap.max_storage_bytes <= 0 or self._store is None:
            return QuotaDecision(status=QuotaStatus.PASS, reason="no_limit")
        used = int(self._store.get_user_storage_bytes(str(self._user.id)) or 0)
        pending = int(_LOCAL_PENDING_STORAGE.get(str(self._user.id), 0))
        total = int(used + pending + int(bytes_count))
        if total > int(snap.max_storage_bytes):
            return QuotaDecision(
                status=QuotaStatus.FAIL,
                reason="storage_bytes_limit",
                limit=int(snap.max_storage_bytes),
                current=int(total),
            )
        return QuotaDecision(status=QuotaStatus.PASS, reason="ok")

    async def reserve_storage_bytes(self, *, bytes_count: int, action: str) -> StorageReservation:
        if bytes_count <= 0:
            return StorageReservation(
                bytes_count=0, user_id=str(self._user.id), backend="none"
            )
        decision = await self.check_storage_bytes(bytes_count=int(bytes_count), action=action)
        if decision.status == QuotaStatus.FAIL:
            _raise_quota(
                user=self._user,
                action=action,
                reason=decision.reason,
                limit=decision.limit,
                current=decision.current,
            )
        async with _LOCAL_LOCK:
            _LOCAL_PENDING_STORAGE[str(self._user.id)] = int(
                _LOCAL_PENDING_STORAGE.get(str(self._user.id), 0) + int(bytes_count)
            )
        return StorageReservation(
            bytes_count=int(bytes_count),
            user_id=str(self._user.id),
            backend="local",
            release_cb=lambda n: self._release_storage_bytes(bytes_count=int(n)),
        )

    async def _release_storage_bytes(self, *, bytes_count: int) -> None:
        if bytes_count <= 0:
            return
        async with _LOCAL_LOCK:
            cur = int(_LOCAL_PENDING_STORAGE.get(str(self._user.id), 0))
            cur = max(0, cur - int(bytes_count))
            if cur <= 0:
                _LOCAL_PENDING_STORAGE.pop(str(self._user.id), None)
            else:
                _LOCAL_PENDING_STORAGE[str(self._user.id)] = cur


async def check_upload_bytes(
    *, request: Request, user: User, total_bytes: int, action: str
) -> QuotaDecision:
    enforcer = QuotaEnforcer.from_request(request=request, user=user)
    return await enforcer.check_upload_bytes(total_bytes=total_bytes, action=action)


async def check_submit_job(
    *,
    request: Request,
    user: User,
    count: int,
    requested_mode: str,
    requested_device: str,
    job_id: str | None,
    action: str,
) -> QuotaDecision:
    enforcer = QuotaEnforcer.from_request(request=request, user=user)
    reservation = await enforcer.reserve_submit_jobs(
        count=count,
        requested_mode=requested_mode,
        requested_device=requested_device,
        job_id=job_id,
        action=action,
    )
    await reservation.release()
    return QuotaDecision(status=QuotaStatus.PASS, reason="ok")


async def check_concurrent_jobs(*, request: Request, user: User, action: str) -> QuotaDecision:
    enforcer = QuotaEnforcer.from_request(request=request, user=user)
    return await enforcer.check_concurrent_jobs(action=action)
