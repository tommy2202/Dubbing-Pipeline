from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, Callable

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.ops import audit
from dubbing_pipeline.runtime.scheduler import Scheduler
from dubbing_pipeline.utils.log import logger

from .fallback_local_queue import FallbackLocalQueue
from .interfaces import QueueBackend, QueueStatus
from .redis_queue import RedisQueue


class AutoQueueBackend(QueueBackend):
    """
    Single canonical queue backend exposed to the app.

    - If Redis is configured+healthy and QUEUE_MODE allows it, use RedisQueue.
    - Otherwise fall back to the existing local Scheduler/JobQueue path.

    This class also provides the UI banner string via QueueStatus.
    """

    def __init__(
        self,
        *,
        scheduler: Scheduler,
        get_store_cb: Callable[[], Any],
        enqueue_job_id_cb: Callable[[str], "asyncio.Future[None] | asyncio.Task[None] | Any"],
    ) -> None:
        s = get_settings()
        self._mode_cfg = str(getattr(s, "queue_mode", "auto") or "auto").strip().lower()
        if self._mode_cfg not in {"auto", "redis", "fallback"}:
            self._mode_cfg = "auto"

        redis_url = str(getattr(s, "redis_url", "") or "").strip()
        self._redis_url = redis_url
        self._scheduler = scheduler

        self._fallback = FallbackLocalQueue(get_store_cb=get_store_cb, scheduler=scheduler)
        def _job_state(job_id: str) -> str:
            st = ""
            store = get_store_cb()
            if store is None:
                return ""
            j = store.get(str(job_id))
            if j is None:
                return ""
            try:
                st = str(getattr(j.state, "value", "") or "")
            except Exception:
                st = ""
            return st

        self._redis: RedisQueue | None = (
            RedisQueue(
                redis_url=redis_url,
                enqueue_job_id_cb=enqueue_job_id_cb,
                get_job_state_cb=_job_state,
            )
            if redis_url
            else None
        )

        self._task: asyncio.Task | None = None
        self._stopping = False

    def _redis_allowed(self) -> bool:
        if self._mode_cfg == "fallback":
            return False
        if self._mode_cfg in {"auto", "redis"}:
            return bool(self._redis is not None)
        return False

    def _redis_active(self) -> bool:
        if not self._redis_allowed():
            return False
        assert self._redis is not None
        st = self._redis.status()
        return bool(st.redis_ok)

    def status(self) -> QueueStatus:
        if self._redis_allowed() and self._redis is not None:
            st = self._redis.status()
            if st.redis_ok and self._mode_cfg != "fallback":
                return st
            # redis configured but unavailable => fallback banner
            return QueueStatus(
                mode="fallback",
                redis_configured=bool(st.redis_configured),
                redis_ok=False,
                detail=st.detail,
                banner="Redis unavailable; using fallback queue",
            )
        # no redis configured or forced fallback
        return QueueStatus(
            mode="fallback",
            redis_configured=bool(self._redis_url),
            redis_ok=False,
            detail="fallback local queue active",
            banner="Redis unavailable; using fallback queue" if self._redis_url else None,
        )

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False

        # Start redis backend if configured (even if it might be unhealthy initially).
        if self._redis is not None:
            with suppress(Exception):
                await self._redis.start()

        # Start fallback scan loop only if redis is not active (or forced fallback).
        if not self._redis_active():
            with suppress(Exception):
                await self._fallback.start()

        # Monitor and toggle fallback scanner based on redis health (auto mode).
        self._task = asyncio.create_task(self._monitor_loop(), name="queue.auto.monitor")
        audit.emit(
            "queue.manager_started",
            request_id=None,
            user_id=None,
            meta={"queue_mode": str(self._mode_cfg), "redis_configured": bool(self._redis_url)},
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        with suppress(Exception):
            await self._fallback.stop()
        if self._redis is not None:
            with suppress(Exception):
                await self._redis.stop()

    async def submit_job(
        self,
        *,
        job_id: str,
        user_id: str,
        mode: str,
        device: str,
        priority: int = 100,
        meta: dict[str, Any] | None = None,
    ) -> None:
        # Prefer redis when healthy; otherwise fallback to scheduler.
        if self._redis_allowed() and self._redis is not None and self._redis_active():
            try:
                await self._redis.submit_job(
                    job_id=job_id,
                    user_id=user_id,
                    mode=mode,
                    device=device,
                    priority=priority,
                    meta=meta,
                )
                return
            except Exception as ex:
                logger.warning(
                    "queue_submit_redis_failed_using_fallback",
                    job_id=str(job_id),
                    user_id=str(user_id),
                    error=str(ex),
                )
        await self._fallback.submit_job(
            job_id=job_id, user_id=user_id, mode=mode, device=device, priority=priority, meta=meta
        )

    async def cancel_job(self, *, job_id: str, user_id: str | None = None) -> None:
        if self._redis is not None:
            with suppress(Exception):
                await self._redis.cancel_job(job_id=job_id, user_id=user_id)
        with suppress(Exception):
            await self._fallback.cancel_job(job_id=job_id, user_id=user_id)

    async def user_counts(self, *, user_id: str) -> dict[str, int]:
        if self._redis_allowed() and self._redis is not None and self._redis_active():
            return await self._redis.user_counts(user_id=user_id)
        return await self._fallback.user_counts(user_id=user_id)

    async def user_quota(self, *, user_id: str) -> dict[str, int] | None:
        if self._redis_allowed() and self._redis is not None and self._redis_active():
            return await self._redis.user_quota(user_id=user_id)
        return await self._fallback.user_quota(user_id=user_id)

    async def admin_snapshot(self, *, limit: int = 200) -> dict[str, Any]:
        if self._redis_allowed() and self._redis is not None and self._redis_active():
            return await self._redis.admin_snapshot(limit=limit)
        return await self._fallback.admin_snapshot(limit=limit)

    async def admin_set_priority(self, *, job_id: str, priority: int) -> bool:
        if self._redis_allowed() and self._redis is not None and self._redis_active():
            return await self._redis.admin_set_priority(job_id=job_id, priority=priority)
        return await self._fallback.admin_set_priority(job_id=job_id, priority=priority)

    async def admin_set_user_quotas(
        self, *, user_id: str, max_running: int | None, max_queued: int | None
    ) -> dict[str, int]:
        if self._redis_allowed() and self._redis is not None and self._redis_active():
            return await self._redis.admin_set_user_quotas(
                user_id=user_id, max_running=max_running, max_queued=max_queued
            )
        return await self._fallback.admin_set_user_quotas(
            user_id=user_id, max_running=max_running, max_queued=max_queued
        )

    async def before_job_run(self, *, job_id: str, user_id: str | None) -> bool:
        if self._redis_allowed() and self._redis is not None and self._redis_active():
            return await self._redis.before_job_run(job_id=job_id, user_id=user_id)
        return await self._fallback.before_job_run(job_id=job_id, user_id=user_id)

    async def after_job_run(
        self,
        *,
        job_id: str,
        user_id: str | None,
        final_state: str,
        ok: bool,
        error: str | None = None,
    ) -> None:
        if self._redis is not None:
            with suppress(Exception):
                await self._redis.after_job_run(
                    job_id=job_id, user_id=user_id, final_state=final_state, ok=ok, error=error
                )
        with suppress(Exception):
            await self._fallback.after_job_run(
                job_id=job_id, user_id=user_id, final_state=final_state, ok=ok, error=error
            )

    async def _monitor_loop(self) -> None:
        """
        Auto-mode health monitor: enables/disables fallback scan loop as Redis flaps.
        """
        last_active = False
        try:
            while not self._stopping:
                active = bool(self._redis_active())
                if self._mode_cfg == "fallback":
                    active = False
                if self._mode_cfg == "redis":
                    # If forced redis, we don't start fallback scanning automatically.
                    active = True if self._redis is not None else False

                if active != last_active:
                    last_active = active
                    if active:
                        # Redis is healthy => stop fallback scan loop (avoid parallel enqueuing).
                        with suppress(Exception):
                            await self._fallback.stop()
                        audit.emit(
                            "queue.manager_mode",
                            request_id=None,
                            user_id=None,
                            meta={"active": "redis"},
                        )
                    else:
                        # Redis down => enable fallback scan loop.
                        with suppress(Exception):
                            await self._fallback.start()
                        audit.emit(
                            "queue.manager_mode",
                            request_id=None,
                            user_id=None,
                            meta={"active": "fallback"},
                        )
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            logger.info("task stopped", task="queue.auto.monitor")
            return

