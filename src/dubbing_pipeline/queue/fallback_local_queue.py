from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Callable

from dubbing_pipeline.jobs.limits import resolve_user_quotas
from dubbing_pipeline.jobs.models import JobState
from dubbing_pipeline.ops import audit
from dubbing_pipeline.runtime.scheduler import JobRecord, Scheduler
from dubbing_pipeline.utils.log import logger

from .interfaces import QueueBackend, QueueStatus


@dataclass(frozen=True, slots=True)
class FallbackConfig:
    scan_interval_s: float


class FallbackLocalQueue(QueueBackend):
    """
    Level 1 fallback backend.

    This preserves existing behavior:
    - job submission schedules directly into the in-proc Scheduler/JobQueue
    - a light poller re-submits any QUEUED jobs (crash-safe single-writer local loop)

    Notes:
    - This does not attempt to be multi-instance safe (it's the fallback when Redis is unavailable).
    - SQLite remains the source of truth for job state.
    """

    def __init__(
        self,
        *,
        get_store_cb: Callable[[], Any],
        scheduler: Scheduler,
    ) -> None:
        self._get_store = get_store_cb
        self._scheduler = scheduler
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._seen: set[str] = set()
        self._cfg = FallbackConfig(scan_interval_s=2.0)

    def status(self) -> QueueStatus:
        return QueueStatus(
            mode="fallback",
            redis_configured=False,
            redis_ok=False,
            detail="fallback local queue active",
            banner="Redis unavailable; using fallback queue",
        )

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._scan_loop(), name="queue.fallback.scan")
        logger.info("queue_backend_started", queue_mode="fallback")
        audit.emit(
            "queue.backend_started",
            request_id=None,
            user_id=None,
            meta={"mode": "fallback"},
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            # asyncio.CancelledError inherits from BaseException in newer Python versions.
            with suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        self._seen.clear()

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
        job_id = str(job_id or "").strip()
        if not job_id:
            return
        self._seen.discard(job_id)
        self._scheduler.submit(
            JobRecord(
                job_id=job_id,
                mode=str(mode or "medium"),
                device_pref=str(device or "auto"),
                created_at=time.time(),
                priority=int(priority),
            )
        )
        logger.info("queue_submit", queue_mode="fallback", job_id=job_id, user_id=str(user_id or ""))
        audit.emit(
            "queue.submit",
            request_id=None,
            user_id=str(user_id or "") or None,
            meta={"mode": "fallback", "job_id": job_id, "priority": int(priority)},
            job_id=job_id,
        )

    async def cancel_job(self, *, job_id: str, user_id: str | None = None) -> None:
        # Local cancel is handled by JobQueue itself; nothing extra required here.
        job_id = str(job_id or "").strip()
        if not job_id:
            return
        logger.info("queue_cancel", queue_mode="fallback", job_id=job_id, user_id=str(user_id or ""))

    async def user_counts(self, *, user_id: str) -> dict[str, int]:
        # Best-effort from SQLite store; used for submission-time policy in fallback mode.
        try:
            store = self._get_store()
            jobs = store.list(limit=2000)
            running = 0
            queued = 0
            today = 0
            uid = str(user_id or "")
            try:
                from dubbing_pipeline.jobs.limits import _same_utc_day  # type: ignore
                from dubbing_pipeline.jobs.models import now_utc

                same_day = _same_utc_day
                now_iso = now_utc()
            except Exception:
                same_day = None
                now_iso = ""
            for j in jobs:
                if str(getattr(j, "owner_id", "") or "") != uid:
                    continue
                st = getattr(j, "state", None)
                v = getattr(st, "value", "") if st is not None else ""
                if v == "RUNNING":
                    running += 1
                if v == "QUEUED":
                    queued += 1
                if same_day and now_iso:
                    try:
                        if same_day(str(getattr(j, "created_at", "") or ""), now_iso):
                            today += 1
                    except Exception:
                        pass
            return {"running": int(running), "queued": int(queued), "today": int(today)}
        except Exception:
            return {"running": 0, "queued": 0, "today": 0}

    async def user_quota(self, *, user_id: str) -> dict[str, int] | None:
        return None

    async def admin_snapshot(self, *, limit: int = 200) -> dict[str, Any]:
        # Fallback view: show SQLite queued/running jobs (best-effort).
        store = self._get_store()
        items: list[dict[str, Any]] = []
        if store is not None:
            for j in store.list(limit=max(1, min(500, int(limit)))):
                items.append(
                    {
                        "job_id": str(j.id),
                        "user_id": str(getattr(j, "owner_id", "") or ""),
                        "mode": str(getattr(j, "mode", "") or ""),
                        "state": str(getattr(getattr(j, "state", None), "value", "") or ""),
                        "priority": None,
                        "queue_mode": "fallback",
                    }
                )
        return {"mode": "fallback", "items": items}

    async def admin_set_priority(self, *, job_id: str, priority: int) -> bool:
        # Priority control is not supported in fallback queue (in-proc scheduler has separate controls).
        return False

    async def admin_set_user_quotas(
        self, *, user_id: str, max_running: int | None, max_queued: int | None
    ) -> dict[str, int]:
        # Not persisted in fallback mode.
        return {}

    async def before_job_run(self, *, job_id: str, user_id: str | None) -> bool:
        # No distributed lock in fallback mode, but enforce per-user concurrency caps.
        uid = str(user_id or "").strip()
        if not uid:
            return True
        store = self._get_store()
        if store is None:
            return True
        try:
            quota = store.get_user_quota(uid) if hasattr(store, "get_user_quota") else {}
            quotas = resolve_user_quotas(overrides=quota)
            max_concurrent = int(quotas.max_concurrent_jobs_per_user or 0)
            if max_concurrent <= 0:
                return True
            jobs = store.list(limit=2000)
            running = 0
            for j in jobs:
                if str(getattr(j, "owner_id", "") or "") != uid:
                    continue
                if getattr(j, "state", None) == JobState.RUNNING:
                    running += 1
            if running >= max_concurrent:
                with suppress(Exception):
                    store.update(job_id, state=JobState.QUEUED, message="Waiting for quota")
                self._seen.discard(str(job_id))
                return False
        except Exception:
            return True
        return True

    async def after_job_run(
        self,
        *,
        job_id: str,
        user_id: str | None,
        final_state: str,
        ok: bool,
        error: str | None = None,
    ) -> None:
        # No-op.
        return

    async def _scan_loop(self) -> None:
        """
        Best-effort crash recovery:
        - periodically re-submit QUEUED jobs into the scheduler
        - avoids resubmitting the same job continuously

        This is intentionally simple to avoid SQLite write contention.
        """
        while not self._stopping:
            try:
                store = self._get_store()
                if store is None:
                    await asyncio.sleep(self._cfg.scan_interval_s)
                    continue
                # Scan a bounded set; store.list already sorts by created_at desc.
                jobs = store.list(limit=250)
                for j in jobs:
                    try:
                        st = getattr(getattr(j, "state", None), "value", "") or ""
                        if str(st).lower() == "queued":
                            jid = str(j.id)
                            if jid and jid not in self._seen:
                                self._seen.add(jid)
                                with suppress(Exception):
                                    self._scheduler.submit(
                                        JobRecord(
                                            job_id=jid,
                                            mode=str(getattr(j, "mode", "medium") or "medium"),
                                            device_pref=str(getattr(j, "device", "auto") or "auto"),
                                            created_at=time.time(),
                                            priority=100,
                                        )
                                    )
                    except Exception:
                        continue
            except asyncio.CancelledError:
                logger.info("task stopped", task="queue.fallback.scan")
                return
            except Exception:
                pass
            await asyncio.sleep(self._cfg.scan_interval_s)

