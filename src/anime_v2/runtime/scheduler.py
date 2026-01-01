from __future__ import annotations

import heapq
import os
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from anime_v2.config import get_settings
from anime_v2.jobs.models import Job, JobState
from anime_v2.jobs.store import JobStore
from anime_v2.runtime import lifecycle
from anime_v2.utils.log import logger


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    mode: str
    device_pref: str
    created_at: float
    priority: int = 100


def _next_lower_mode(mode: str) -> str:
    m = (mode or "medium").lower().strip()
    if m == "high":
        return "medium"
    if m == "medium":
        return "low"
    return "low"


class _RedisMutex:
    def __init__(self, url: str, name: str) -> None:
        self.url = url
        self.name = name
        self._client = None

    def _redis(self):
        if self._client is not None:
            return self._client
        try:
            import redis  # type: ignore

            self._client = redis.Redis.from_url(self.url, decode_responses=True)
            return self._client
        except Exception:
            return None

    @contextmanager
    def acquire(self, *, ttl_s: int = 10) -> Iterator[None]:
        r = self._redis()
        if r is None:
            yield
            return
        token = f"{os.getpid()}:{threading.get_ident()}:{random.random()}"
        ok = False
        try:
            ok = bool(r.set(self.name, token, nx=True, ex=ttl_s))
        except Exception:
            ok = False
        try:
            if ok:
                yield
            else:
                # no lock => skip critical section (best-effort)
                yield
        finally:
            if ok:
                try:
                    # best-effort release
                    if r.get(self.name) == token:
                        r.delete(self.name)
                except Exception:
                    pass


class Scheduler:
    """
    In-process priority scheduler for job execution + phase concurrency.

    - submit() adds JobRecord into a priority/delay heap
    - dispatcher thread enqueues jobs into JobQueue only when global capacity allows
    - JobQueue uses `phase()` context managers to obey per-phase concurrency caps
    - optional Redis mutex reduces multi-instance scheduling stampede (best-effort)
    """

    _singleton: "Scheduler | None" = None
    _singleton_lock = threading.Lock()

    def __init__(self, *, store: JobStore, enqueue_cb) -> None:
        self.store = store
        self._enqueue_cb = enqueue_cb  # callable(job: Job) -> None (thread-safe)
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._seq = 0
        self._heap: list[tuple[float, int, float, int, JobRecord]] = []
        self._stop = False

        s = get_settings()
        self._max_global = max(1, int(s.max_concurrency_global))
        self._max_transcribe = max(1, int(s.max_concurrency_transcribe))
        self._max_tts = max(1, int(s.max_concurrency_tts))
        # Allow disabling backpressure by setting BACKPRESSURE_Q_MAX=-1
        try:
            self._bp_qmax = int(s.backpressure_q_max)
        except Exception:
            self._bp_qmax = 6

        self._active_global = 0
        self._active_phase: dict[str, int] = {"audio": 0, "transcribe": 0, "tts": 0, "mux": 0}
        self._phase_sem: dict[str, threading.Semaphore] = {
            "audio": threading.Semaphore(max(1, self._max_global)),
            "transcribe": threading.Semaphore(self._max_transcribe),
            "tts": threading.Semaphore(self._max_tts),
            "mux": threading.Semaphore(max(1, self._max_global)),
        }

        self._redis_mutex = _RedisMutex(s.redis_url, "anime_v2:scheduler:dispatch") if s.redis_url else None
        self._thread = threading.Thread(target=self._dispatch_loop, name="anime_v2.scheduler", daemon=True)

    @classmethod
    def install(cls, sched: "Scheduler") -> None:
        with cls._singleton_lock:
            cls._singleton = sched

    @classmethod
    def instance_optional(cls) -> "Scheduler | None":
        return cls._singleton

    @classmethod
    def instance(cls) -> "Scheduler":
        s = cls._singleton
        if s is None:
            raise RuntimeError("Scheduler not installed")
        return s

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()
            logger.info("scheduler_started", max_global=self._max_global, transcribe=self._max_transcribe, tts=self._max_tts, bp_qmax=self._bp_qmax)

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    def submit(self, job: JobRecord) -> None:
        """
        Add a job record to scheduling queue.
        Backpressure policy:
          - if queue length > BACKPRESSURE_Q_MAX:
              - degrade mode high->medium->low
              - if already low, delay enqueue with jitter
        """
        if lifecycle.is_draining():
            raise RuntimeError("draining")
        with self._cv:
            qlen = len(self._heap)
            mode = (job.mode or "medium").lower().strip()
            available_at = time.monotonic()

            if self._bp_qmax >= 0 and qlen > self._bp_qmax:
                new_mode = _next_lower_mode(mode)
                if new_mode != mode:
                    logger.info("scheduler_backpressure_degrade", job_id=job.job_id, from_mode=mode, to_mode=new_mode, qlen=qlen)
                    mode = new_mode
                    # reflect mode into persisted job
                    try:
                        self.store.update(job.job_id, mode=mode, message=f"Backpressure: degraded to {mode}")
                    except Exception:
                        pass
                else:
                    # already low => delay with jitter/backoff
                    delay = min(30.0, 0.5 + (qlen - max(0, self._bp_qmax)) * 0.75 + random.random() * 0.75)
                    available_at += delay
                    logger.info("scheduler_backpressure_delay", job_id=job.job_id, delay_s=delay, qlen=qlen)
                    try:
                        self.store.update(job.job_id, message=f"Backpressure: delayed {delay:.1f}s")
                    except Exception:
                        pass

            rec = JobRecord(job_id=job.job_id, mode=mode, device_pref=job.device_pref, created_at=job.created_at, priority=job.priority)
            self._seq += 1
            heapq.heappush(self._heap, (available_at, int(rec.priority), float(rec.created_at), self._seq, rec))
            self._cv.notify_all()

    def on_job_done(self, job_id: str) -> None:
        with self._cv:
            self._active_global = max(0, int(self._active_global) - 1)
            self._cv.notify_all()

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        name = str(name)
        sem = self._phase_sem.get(name)
        if sem is None:
            yield
            return
        sem.acquire()
        try:
            with self._cv:
                self._active_phase[name] = int(self._active_phase.get(name, 0)) + 1
            yield
        finally:
            with self._cv:
                self._active_phase[name] = max(0, int(self._active_phase.get(name, 0)) - 1)
            sem.release()

    def state(self) -> dict[str, Any]:
        with self._cv:
            return {
                "queue_len": len(self._heap),
                "active_global": int(self._active_global),
                "limits": {
                    "max_global": int(self._max_global),
                    "max_transcribe": int(self._max_transcribe),
                    "max_tts": int(self._max_tts),
                    "backpressure_q_max": int(self._bp_qmax),
                },
                "active_phase": dict(self._active_phase),
            }

    def _dispatch_loop(self) -> None:
        while True:
            with self._cv:
                if self._stop:
                    return
                now = time.monotonic()
                if not self._heap:
                    self._cv.wait(timeout=0.5)
                    continue
                available_at, _, _, _, rec = self._heap[0]
                if available_at > now:
                    self._cv.wait(timeout=min(0.5, available_at - now))
                    continue
                if self._active_global >= self._max_global:
                    self._cv.wait(timeout=0.25)
                    continue
                heapq.heappop(self._heap)
                self._active_global += 1

            # Optional redis mutex (best-effort)
            if self._redis_mutex is not None:
                with self._redis_mutex.acquire(ttl_s=10):
                    self._enqueue_one(rec)
            else:
                self._enqueue_one(rec)

    def _enqueue_one(self, rec: JobRecord) -> None:
        job = self.store.get(rec.job_id)
        if job is None:
            self.on_job_done(rec.job_id)
            return
        if job.state != JobState.QUEUED:
            self.on_job_done(rec.job_id)
            return
        try:
            # Ensure mode/device are up-to-date in store record
            if rec.mode and rec.mode != job.mode:
                job = self.store.update(rec.job_id, mode=rec.mode) or job
            if rec.device_pref and rec.device_pref != job.device:
                job = self.store.update(rec.job_id, device=rec.device_pref) or job
        except Exception:
            pass

        try:
            self._enqueue_cb(job)
            logger.info("scheduler_enqueued", job_id=rec.job_id, mode=job.mode, device=job.device)
        except Exception as ex:
            logger.warning("scheduler_enqueue_failed", job_id=rec.job_id, error=str(ex))
            try:
                self.store.update(rec.job_id, state=JobState.FAILED, message="Scheduler enqueue failed", error=str(ex))
            except Exception:
                pass
            self.on_job_done(rec.job_id)

