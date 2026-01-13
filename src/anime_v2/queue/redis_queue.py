from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Callable

from anime_v2.api.models import Role
from anime_v2.config import get_settings
from anime_v2.jobs.policy import evaluate_dispatch
from anime_v2.ops import audit
from anime_v2.utils.log import logger

from .interfaces import QueueBackend, QueueStatus


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class RedisQueueConfig:
    prefix: str
    consumer: str
    lock_ttl_ms: int
    lock_refresh_ms: int
    max_attempts: int
    base_backoff_ms: int
    backoff_cap_ms: int
    cancel_ttl_ms: int
    active_set_ttl_ms: int


class RedisQueue(QueueBackend):
    """
    Level 2 queue backend (Redis).

    Redis is the source of truth for:
    - queue state (pending/delayed/running)
    - per-job distributed locks
    - per-user counters/quotas

    SQLite remains the source of truth for job metadata and final job state.
    """

    def __init__(
        self,
        *,
        redis_url: str,
        enqueue_job_id_cb: Callable[[str], "asyncio.Future[None] | asyncio.Task[None] | Any"],
        get_job_state_cb: Callable[[str], str] | None = None,
    ) -> None:
        self._redis_url = str(redis_url or "").strip()
        self._enqueue_cb = enqueue_job_id_cb
        self._get_job_state = get_job_state_cb
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        self._healthy = False

        self._lock_token_by_job: dict[str, str] = {}
        self._lock_refresh_task_by_job: dict[str, asyncio.Task] = {}
        self._claimed_user_by_job: dict[str, str] = {}
        self._claimed_mode_by_job: dict[str, str] = {}
        self._claimed_role_by_job: dict[str, str] = {}

        s = get_settings()
        prefix = str(getattr(s, "redis_queue_prefix", "dp") or "dp").strip().strip(":") or "dp"
        self._cfg = RedisQueueConfig(
            prefix=prefix,
            consumer=f"{prefix}:{_consumer_id()}",
            lock_ttl_ms=max(10_000, int(getattr(s, "redis_lock_ttl_ms", 300_000) or 300_000)),
            lock_refresh_ms=max(2_000, int(getattr(s, "redis_lock_refresh_ms", 20_000) or 20_000)),
            max_attempts=max(1, int(getattr(s, "redis_queue_max_attempts", 8) or 8)),
            base_backoff_ms=max(250, int(getattr(s, "redis_queue_backoff_ms", 750) or 750)),
            backoff_cap_ms=max(2_000, int(getattr(s, "redis_queue_backoff_cap_ms", 30_000) or 30_000)),
            cancel_ttl_ms=max(10_000, int(getattr(s, "redis_cancel_ttl_ms", 24 * 3600_000) or 24 * 3600_000)),
            active_set_ttl_ms=max(
                30_000, int(getattr(s, "redis_active_set_ttl_ms", 6 * 3600_000) or 6 * 3600_000)
            ),
        )

        self._client = None
        self._claim_lua = None

    def _redis(self):
        if self._client is not None:
            return self._client
        try:
            import redis.asyncio as redis  # type: ignore

            self._client = redis.Redis.from_url(self._redis_url, decode_responses=True)
            return self._client
        except Exception as ex:
            # Do not log the URL (may contain credentials).
            logger.warning("queue.redis_init_failed", error=str(ex))
            return None

    def status(self) -> QueueStatus:
        configured = bool(self._redis_url)
        ok = bool(self._healthy) if configured else False
        if not configured:
            return QueueStatus(
                mode="fallback",
                redis_configured=False,
                redis_ok=False,
                detail="REDIS_URL not set; using fallback queue",
                banner="Redis not configured; using fallback queue",
            )
        if ok:
            return QueueStatus(
                mode="redis",
                redis_configured=True,
                redis_ok=True,
                detail="redis queue active",
                banner=None,
            )
        return QueueStatus(
            mode="fallback",
            redis_configured=True,
            redis_ok=False,
            detail="redis unavailable; using fallback queue",
            banner="Redis unavailable; using fallback queue",
        )

    async def start(self) -> None:
        if self._tasks:
            return
        self._stopping = False
        self._healthy = False

        await self._prepare_scripts()

        self._tasks.append(asyncio.create_task(self._health_loop(), name="queue.redis.health"))
        self._tasks.append(asyncio.create_task(self._consume_loop(), name="queue.redis.consume"))
        self._tasks.append(asyncio.create_task(self._delayed_mover_loop(), name="queue.redis.delayed"))

        logger.info("queue_backend_started", queue_mode="redis", prefix=self._cfg.prefix)
        audit.emit(
            "queue.backend_started",
            request_id=None,
            user_id=None,
            meta={"mode": "redis", "prefix": self._cfg.prefix},
        )

    async def stop(self) -> None:
        self._stopping = True
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

        # Stop any per-job lock refresh tasks.
        for t in list(self._lock_refresh_task_by_job.values()):
            t.cancel()
        if self._lock_refresh_task_by_job:
            await asyncio.gather(*self._lock_refresh_task_by_job.values(), return_exceptions=True)
        self._lock_refresh_task_by_job.clear()

        self._lock_token_by_job.clear()
        self._claimed_user_by_job.clear()
        self._claimed_mode_by_job.clear()
        self._claimed_role_by_job.clear()

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
        r = self._redis()
        if r is None:
            raise RuntimeError("redis_unavailable")

        # Record job metadata in redis (non-sensitive).
        role = ""
        if isinstance(meta, dict):
            with suppress(Exception):
                role = str(meta.get("user_role") or "")
        await r.hset(
            self._job_meta_key(job_id),
            mapping={
                "job_id": job_id,
                "user_id": str(user_id or ""),
                "user_role": str(role or ""),
                "mode": str(mode or ""),
                "device": str(device or ""),
                "priority": str(int(priority)),
                "created_ms": str(_now_ms()),
            },
        )
        with suppress(Exception):
            await r.pexpire(self._job_meta_key(job_id), self._cfg.active_set_ttl_ms)

        # Add to pending priority queue and per-user queued set.
        await r.zadd(self._pending_key(), {job_id: float(int(priority))})
        if str(user_id or "").strip():
            await r.sadd(self._user_queued_set_key(str(user_id)), job_id)
            with suppress(Exception):
                await r.pexpire(self._user_queued_set_key(str(user_id)), self._cfg.active_set_ttl_ms)

        logger.info("queue_submit", queue_mode="redis", job_id=job_id, user_id=str(user_id or ""), mode=str(mode or ""))
        audit.emit(
            "queue.submit",
            request_id=None,
            user_id=str(user_id or "") or None,
            meta={"mode": "redis", "job_id": job_id, "priority": int(priority)},
            job_id=job_id,
        )

    async def cancel_job(self, *, job_id: str, user_id: str | None = None) -> None:
        job_id = str(job_id or "").strip()
        if not job_id:
            return
        r = self._redis()
        if r is None:
            return
        key = self._cancel_key(job_id)
        try:
            await r.set(key, "1", px=self._cfg.cancel_ttl_ms)
            # Best-effort: remove from pending and delayed queues.
            with suppress(Exception):
                await r.zrem(self._delayed_key(), job_id)
            with suppress(Exception):
                await r.zrem(self._pending_key(), job_id)
            # Keep counters tidy.
            uid = str(user_id or "").strip()
            if uid:
                with suppress(Exception):
                    await r.srem(self._user_queued_set_key(uid), job_id)
            logger.info(
                "queue_cancel_flag_set",
                queue_mode="redis",
                job_id=job_id,
                user_id=str(user_id or ""),
            )
            audit.emit(
                "queue.cancel_flag_set",
                request_id=None,
                user_id=str(user_id) if user_id else None,
                meta={"mode": "redis", "job_id": job_id},
                job_id=job_id,
            )
        except Exception as ex:
            logger.warning("queue_cancel_flag_failed", queue_mode="redis", job_id=job_id, error=str(ex))

    async def before_job_run(self, *, job_id: str, user_id: str | None) -> bool:
        """
        Acquire distributed lock + apply per-user active cap.
        Returns True if the job is allowed to run on this worker now.
        """
        job_id = str(job_id or "").strip()
        if not job_id:
            return False
        r = self._redis()
        if r is None:
            # If redis is down mid-run, refuse to start new work in redis mode.
            return False

        # Terminal job states (SQLite source of truth) should never be re-run.
        if self._get_job_state is not None:
            with suppress(Exception):
                st = str(self._get_job_state(job_id) or "")
                if st in {"DONE", "FAILED", "CANCELED"}:
                    await self._release_and_cleanup(job_id, reason=f"terminal_state:{st}")
                    return False

        # Cancellation check (fast path)
        try:
            if await r.get(self._cancel_key(job_id)):
                logger.info("queue_skip_canceled", queue_mode="redis", job_id=job_id)
                await self._nack_current(job_id, reason="canceled")
                return False
        except Exception:
            pass

        # Dispatch-time policy safety net (canonical policy module).
        meta = await self._read_meta(job_id)
        uid = str(meta.get("user_id") or (user_id or "")).strip()
        mode = str(meta.get("mode") or "").strip().lower() or "medium"
        role_s = str(meta.get("user_role") or "").strip().lower()
        role = Role.admin if role_s == "admin" else Role.operator

        counts = await self.user_counts(user_id=uid)
        high_running = int(await r.scard(self._running_high_set_key()) or 0)
        quota = await self.user_quota(user_id=uid)
        dec = evaluate_dispatch(
            user_id=uid,
            user_role=role,
            requested_mode=mode,
            running=int(counts.get("running") or 0),
            queued=int(counts.get("queued") or 0),
            global_high_running=high_running,
            user_quota=quota,
            job_id=job_id,
        )
        if not dec.ok:
            # Defer with backoff; release lock so others can attempt later.
            await self._defer(job_id, reason=",".join(dec.reasons) or "policy")
            await self._release_and_cleanup(job_id, reason="policy_denied")
            return False

        # Lock is typically acquired during claim (atomically removing from pending).
        token = self._lock_token_by_job.get(job_id, "") or ""
        if not token:
            token = _lock_token(job_id)
            if not await self._acquire_lock(job_id, token):
                await self._defer(job_id, reason="lock_busy")
                return False
            self._lock_token_by_job[job_id] = token
            self._lock_refresh_task_by_job[job_id] = asyncio.create_task(
                self._lock_refresh_loop(job_id=job_id, token=str(token)),
                name=f"queue.redis.lock_refresh:{job_id}",
            )

        # Mark local lock token and keep it alive.
        self._lock_token_by_job[job_id] = token
        self._claimed_user_by_job[job_id] = uid
        self._claimed_mode_by_job[job_id] = mode
        self._claimed_role_by_job[job_id] = role_s
        if uid:
            with suppress(Exception):
                await r.sadd(self._user_running_set_key(uid), job_id)
                await r.srem(self._user_queued_set_key(uid), job_id)
                await r.pexpire(self._user_running_set_key(uid), self._cfg.active_set_ttl_ms)
            with suppress(Exception):
                await r.sadd(self._running_set_key(), job_id)
                await r.pexpire(self._running_set_key(), self._cfg.active_set_ttl_ms)
            if mode == "high":
                with suppress(Exception):
                    await r.sadd(self._running_high_set_key(), job_id)
                    await r.pexpire(self._running_high_set_key(), self._cfg.active_set_ttl_ms)

        self._lock_refresh_task_by_job[job_id] = asyncio.create_task(
            self._lock_refresh_loop(job_id=job_id, token=token),
            name=f"queue.redis.lock_refresh:{job_id}",
        )

        logger.info(
            "queue_lock_acquired",
            queue_mode="redis",
            job_id=job_id,
            user_id=uid,
            lock_acquired=True,
        )
        audit.emit(
            "queue.lock_acquired",
            request_id=None,
            user_id=uid or None,
            meta={"mode": "redis", "job_id": job_id},
            job_id=job_id,
        )
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
        job_id = str(job_id or "").strip()
        if not job_id:
            return
        uid = str(user_id or "").strip() or self._claimed_user_by_job.get(job_id, "")
        mode = str(self._claimed_mode_by_job.get(job_id, "") or "")

        # Stop lock refresh
        t = self._lock_refresh_task_by_job.pop(job_id, None)
        if t is not None:
            t.cancel()
            with suppress(Exception):
                await t

        r = self._redis()
        if r is not None and uid:
            with suppress(Exception):
                await r.srem(self._user_running_set_key(uid), job_id)
            with suppress(Exception):
                await r.srem(self._running_set_key(), job_id)
            if mode == "high":
                with suppress(Exception):
                    await r.srem(self._running_high_set_key(), job_id)

        # Release lock
        token = self._lock_token_by_job.pop(job_id, "")
        if r is not None and token:
            with suppress(Exception):
                await self._release_lock(job_id, token)

        self._claimed_user_by_job.pop(job_id, None)
        self._claimed_mode_by_job.pop(job_id, None)
        self._claimed_role_by_job.pop(job_id, None)

        logger.info(
            "queue_job_done",
            queue_mode="redis",
            job_id=job_id,
            user_id=uid,
            final_state=str(final_state or ""),
            ok=bool(ok),
            attempt=int(await self._attempts(job_id) or 0) if r is not None else 0,
        )
        audit.emit(
            "queue.job_done",
            request_id=None,
            user_id=uid or None,
            meta={
                "mode": "redis",
                "job_id": job_id,
                "final_state": str(final_state or ""),
                "ok": bool(ok),
                "error": str(error or "")[:200] if error else None,
            },
            job_id=job_id,
        )

    async def user_counts(self, *, user_id: str) -> dict[str, int]:
        uid = str(user_id or "").strip()
        if not uid:
            return {"running": 0, "queued": 0, "today": 0}
        r = self._redis()
        if r is None:
            return {"running": 0, "queued": 0, "today": 0}
        try:
            running = int(await r.scard(self._user_running_set_key(uid)) or 0)
            queued = int(await r.scard(self._user_queued_set_key(uid)) or 0)
            return {"running": running, "queued": queued, "today": 0}
        except Exception:
            return {"running": 0, "queued": 0, "today": 0}

    async def user_quota(self, *, user_id: str) -> dict[str, int] | None:
        uid = str(user_id or "").strip()
        if not uid:
            return None
        r = self._redis()
        if r is None:
            return None
        try:
            h = await r.hgetall(self._user_quota_key(uid))
            if not isinstance(h, dict) or not h:
                return None
            out: dict[str, int] = {}
            if "max_running" in h:
                out["max_running"] = int(h["max_running"])
            if "max_queued" in h:
                out["max_queued"] = int(h["max_queued"])
            return out
        except Exception:
            return None

    async def admin_snapshot(self, *, limit: int = 200) -> dict[str, Any]:
        r = self._redis()
        if r is None:
            return {"mode": "redis", "ok": False, "detail": "redis unavailable"}
        lim = max(1, min(500, int(limit)))
        pending = []
        try:
            items = await r.zrevrange(self._pending_key(), 0, lim - 1, withscores=True)
            for job_id, score in items:
                meta = await self._read_meta(str(job_id))
                pending.append(
                    {
                        "job_id": str(job_id),
                        "priority": int(score or 0),
                        "user_id": str(meta.get("user_id") or ""),
                        "mode": str(meta.get("mode") or ""),
                        "state": "QUEUED",
                    }
                )
        except Exception:
            pending = []

        running = []
        try:
            rset = await r.smembers(self._running_set_key())
            for job_id in sorted([str(x) for x in (rset or set())]):
                meta = await self._read_meta(job_id)
                running.append(
                    {
                        "job_id": job_id,
                        "priority": int(meta.get("priority") or 0) if str(meta.get("priority") or "").isdigit() else None,
                        "user_id": str(meta.get("user_id") or ""),
                        "mode": str(meta.get("mode") or ""),
                        "state": "RUNNING",
                    }
                )
        except Exception:
            running = []
        return {
            "mode": "redis",
            "ok": True,
            "pending": pending,
            "running": running,
            "counts": {
                "pending": len(pending),
                "running": len(running),
                "running_high": int(await r.scard(self._running_high_set_key()) or 0),
            },
        }

    async def admin_set_priority(self, *, job_id: str, priority: int) -> bool:
        jid = str(job_id or "").strip()
        if not jid:
            return False
        r = self._redis()
        if r is None:
            return False
        pr = max(0, min(1000, int(priority)))
        # Only meaningful if job is still pending.
        try:
            if await r.zscore(self._pending_key(), jid) is None:
                return False
            await r.zadd(self._pending_key(), {jid: float(pr)})
            with suppress(Exception):
                await r.hset(self._job_meta_key(jid), mapping={"priority": str(int(pr))})
            return True
        except Exception:
            return False

    async def admin_set_user_quotas(
        self, *, user_id: str, max_running: int | None, max_queued: int | None
    ) -> dict[str, int]:
        uid = str(user_id or "").strip()
        if not uid:
            return {}
        r = self._redis()
        if r is None:
            return {}
        m: dict[str, str] = {}
        if max_running is not None:
            m["max_running"] = str(max(0, int(max_running)))
        if max_queued is not None:
            m["max_queued"] = str(max(0, int(max_queued)))
        if not m:
            return {}
        await r.hset(self._user_quota_key(uid), mapping=m)
        return {k: int(v) for k, v in m.items()}

    async def _health_loop(self) -> None:
        while not self._stopping:
            r = self._redis()
            ok = False
            if r is not None:
                try:
                    pong = await r.ping()
                    ok = bool(pong)
                except Exception:
                    ok = False
            self._healthy = bool(ok)
            await asyncio.sleep(2.0)

    async def _consume_loop(self) -> None:
        """
        Claim jobs from Redis pending queue and enqueue into the local executor.

        Crash safety:
        - A job is removed from pending only when a lock has been acquired.
        - If the worker crashes, lock TTL expiry makes the job re-queueable by admin/poller tools.
        """
        r = self._redis()
        if r is None:
            return
        while not self._stopping:
            try:
                job_id = await self._claim_one()
                if not job_id:
                    await asyncio.sleep(0.25)
                    continue
                # Attempts + max attempts
                attempt = 0
                with suppress(Exception):
                    attempt = int(await r.hincrby(self._job_meta_key(job_id), "attempts", 1))
                if attempt and attempt > int(self._cfg.max_attempts):
                    await self._send_to_dlq(job_id=job_id, reason=f"max_attempts_exceeded:{attempt}")
                    await self._release_and_cleanup(job_id, reason="max_attempts")
                    continue

                logger.info(
                    "queue_claimed",
                    queue_mode="redis",
                    job_id=job_id,
                    user_id=str((await self._read_meta(job_id)).get("user_id") or ""),
                    attempt=int(attempt or 0),
                )
                audit.emit(
                    "queue.claimed",
                    request_id=None,
                    user_id=str((await self._read_meta(job_id)).get("user_id") or "") or None,
                    meta={"mode": "redis", "job_id": job_id, "attempt": int(attempt or 0)},
                    job_id=job_id,
                )

                res = self._enqueue_cb(job_id)
                if asyncio.iscoroutine(res):
                    await res
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                logger.warning("queue_consume_error", queue_mode="redis", error=str(ex))
                await asyncio.sleep(1.0)

    async def _delayed_mover_loop(self) -> None:
        """
        Move delayed jobs (zset) back into the stream when due.
        """
        r = self._redis()
        if r is None:
            return
        while not self._stopping:
            try:
                now = float(time.time())
                due = await r.zrangebyscore(self._delayed_key(), min="-inf", max=now, start=0, num=50)
                if due:
                    for job_id in due:
                        jid = str(job_id)
                        with suppress(Exception):
                            await r.zrem(self._delayed_key(), jid)
                        meta = await self._read_meta(jid)
                        pr = int(meta.get("priority") or 100) if str(meta.get("priority") or "").isdigit() else 100
                        with suppress(Exception):
                            await r.zadd(self._pending_key(), {jid: float(pr)})
                await asyncio.sleep(0.5 if due else 1.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(2.0)

    async def _attempts(self, job_id: str) -> int:
        r = self._redis()
        if r is None:
            return 0
        try:
            v = await r.hget(self._job_meta_key(job_id), "attempts")
            return int(v or 0)
        except Exception:
            return 0

    async def _defer(self, job_id: str, *, reason: str) -> None:
        r = self._redis()
        if r is None:
            return
        attempt = await self._attempts(job_id)
        att = max(1, int(attempt or 1))
        delay_ms = min(self._cfg.backoff_cap_ms, self._cfg.base_backoff_ms * (2 ** max(0, att - 1)))
        due = time.time() + (float(delay_ms) / 1000.0)
        with suppress(Exception):
            await r.zadd(self._delayed_key(), {str(job_id): float(due)})
        logger.info(
            "queue_deferred",
            queue_mode="redis",
            job_id=str(job_id),
            attempt=int(attempt or 0),
            delay_ms=int(delay_ms),
            reason=str(reason),
        )
        audit.emit(
            "queue.deferred",
            request_id=None,
            user_id=None,
            meta={"mode": "redis", "job_id": str(job_id), "attempt": int(attempt or 0), "reason": str(reason)},
            job_id=str(job_id),
        )

    async def _send_to_dlq(self, *, job_id: str, reason: str) -> None:
        r = self._redis()
        if r is None:
            return
        meta = await self._read_meta(job_id)
        uid = str(meta.get("user_id") or "")
        # Optional dead-letter list.
        with suppress(Exception):
            await r.lpush(self._dlq_key(), f"{job_id}|{uid}|{reason}|{_now_ms()}")
        logger.warning(
            "queue_dead_letter",
            queue_mode="redis",
            job_id=str(job_id),
            user_id=str(uid or ""),
            reason=str(reason),
        )
        audit.emit(
            "queue.dead_letter",
            request_id=None,
            user_id=str(uid or "") or None,
            meta={"mode": "redis", "job_id": str(job_id), "reason": str(reason)},
            job_id=str(job_id),
        )

    async def _acquire_lock(self, job_id: str, token: str) -> bool:
        r = self._redis()
        if r is None:
            return False
        key = self._lock_key(job_id)
        try:
            ok = await r.set(key, token, nx=True, px=int(self._cfg.lock_ttl_ms))
            return bool(ok)
        except Exception:
            return False

    async def _release_lock(self, job_id: str, token: str) -> None:
        """
        Best-effort safe release (only delete if token matches).
        """
        r = self._redis()
        if r is None:
            return
        key = self._lock_key(job_id)
        lua = """
        if redis.call("GET", KEYS[1]) == ARGV[1] then
          return redis.call("DEL", KEYS[1])
        else
          return 0
        end
        """
        with suppress(Exception):
            await r.eval(lua, 1, key, token)

    async def _lock_refresh_loop(self, *, job_id: str, token: str) -> None:
        r = self._redis()
        if r is None:
            return
        key = self._lock_key(job_id)
        lua = """
        if redis.call("GET", KEYS[1]) == ARGV[1] then
          return redis.call("PEXPIRE", KEYS[1], ARGV[2])
        else
          return 0
        end
        """
        while not self._stopping:
            try:
                await asyncio.sleep(float(self._cfg.lock_refresh_ms) / 1000.0)
                # Only refresh if token still matches.
                await r.eval(lua, 1, key, token, str(int(self._cfg.lock_ttl_ms)))
            except asyncio.CancelledError:
                raise
            except Exception:
                # keep trying; lock may be gone (e.g., operator deleted it)
                continue

    async def _prepare_scripts(self) -> None:
        r = self._redis()
        if r is None:
            return
        # Lua: claim one job from pending by priority and acquire lock.
        # KEYS[1]=pending_zset, KEYS[2]=lock_key_prefix
        # ARGV[1]=lock_ttl_ms, ARGV[2]=token
        lua = """
        local pending = KEYS[1]
        local lock_prefix = KEYS[2]
        local ttl = tonumber(ARGV[1])
        local token = ARGV[2]
        local items = redis.call('ZREVRANGE', pending, 0, 0)
        if (not items) or (#items == 0) then
          return nil
        end
        local job_id = items[1]
        local lock_key = lock_prefix .. job_id .. ':lock'
        local ok = redis.call('SET', lock_key, token, 'NX', 'PX', ttl)
        if not ok then
          return nil
        end
        redis.call('ZREM', pending, job_id)
        return job_id
        """
        try:
            self._claim_lua = await r.script_load(lua)
        except Exception:
            self._claim_lua = None

    async def _claim_one(self) -> str:
        r = self._redis()
        if r is None:
            return ""
        token = _lock_token("claim")
        try:
            if self._claim_lua:
                jid = await r.evalsha(
                    self._claim_lua,
                    2,
                    self._pending_key(),
                    self._lock_key_prefix(),
                    str(int(self._cfg.lock_ttl_ms)),
                    str(token),
                )
            else:
                jid = None
            job_id = str(jid or "").strip()
            if not job_id:
                return ""
            # Remember token; before_job_run will convert this into a running lease/counters update.
            self._lock_token_by_job[job_id] = str(token)
            return job_id
        except Exception:
            return ""

    async def _read_meta(self, job_id: str) -> dict[str, Any]:
        r = self._redis()
        if r is None:
            return {}
        try:
            m = await r.hgetall(self._job_meta_key(job_id))
            return dict(m) if isinstance(m, dict) else {}
        except Exception:
            return {}

    async def _release_and_cleanup(self, job_id: str, *, reason: str) -> None:
        r = self._redis()
        if r is None:
            return
        uid = self._claimed_user_by_job.get(job_id, "")
        mode = self._claimed_mode_by_job.get(job_id, "")
        if uid:
            with suppress(Exception):
                await r.srem(self._user_running_set_key(uid), job_id)
                await r.srem(self._user_queued_set_key(uid), job_id)
        with suppress(Exception):
            await r.srem(self._running_set_key(), job_id)
        if mode == "high":
            with suppress(Exception):
                await r.srem(self._running_high_set_key(), job_id)
        token = self._lock_token_by_job.get(job_id, "")
        if token:
            with suppress(Exception):
                await self._release_lock(job_id, token)
        logger.info("queue_release", queue_mode="redis", job_id=str(job_id), reason=str(reason))

    def _pending_key(self) -> str:
        return f"{self._cfg.prefix}:queue:pending"

    def _delayed_key(self) -> str:
        return f"{self._cfg.prefix}:queue:delayed"

    def _running_set_key(self) -> str:
        return f"{self._cfg.prefix}:queue:running"

    def _running_high_set_key(self) -> str:
        return f"{self._cfg.prefix}:queue:running:high"

    def _dlq_key(self) -> str:
        return f"{self._cfg.prefix}:queue:dlq"

    def _lock_key_prefix(self) -> str:
        return f"{self._cfg.prefix}:job:"

    def _lock_key(self, job_id: str) -> str:
        return f"{self._cfg.prefix}:job:{job_id}:lock"

    def _cancel_key(self, job_id: str) -> str:
        return f"{self._cfg.prefix}:job:{job_id}:cancel"

    def _job_meta_key(self, job_id: str) -> str:
        return f"{self._cfg.prefix}:job:{job_id}:meta"

    def _user_running_set_key(self, user_id: str) -> str:
        return f"{self._cfg.prefix}:user:{user_id}:running"

    def _user_queued_set_key(self, user_id: str) -> str:
        return f"{self._cfg.prefix}:user:{user_id}:queued"

    def _user_quota_key(self, user_id: str) -> str:
        return f"{self._cfg.prefix}:user:{user_id}:quota"


def _consumer_id() -> str:
    import os
    import socket

    host = socket.gethostname()
    return f"{host}:{os.getpid()}"


def _lock_token(job_id: str) -> str:
    import os
    import secrets
    import socket

    return f"{socket.gethostname()}:{os.getpid()}:{job_id}:{secrets.token_hex(8)}"

