from __future__ import annotations

import asyncio
import builtins
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import JobState, now_utc
from dubbing_pipeline.ops.storage import periodic_prune_tick
from dubbing_pipeline.runtime.model_manager import ModelManager
from dubbing_pipeline.utils.log import logger

_draining = threading.Event()
_deadline_lock = threading.Lock()
_deadline_at: float | None = None


@dataclass(frozen=True, slots=True)
class DrainState:
    draining: bool
    deadline_at: float | None
    remaining_sec: int | None


def is_draining() -> bool:
    return _draining.is_set()


def drain_state() -> DrainState:
    with _deadline_lock:
        dl = _deadline_at
    if not is_draining():
        return DrainState(draining=False, deadline_at=dl, remaining_sec=None)
    if dl is None:
        return DrainState(draining=True, deadline_at=None, remaining_sec=None)
    rem = max(0, int(dl - time.time()))
    return DrainState(draining=True, deadline_at=dl, remaining_sec=rem)


def begin_draining(*, timeout_sec: int = 120) -> DrainState:
    """
    Enter draining mode:
    - stop accepting new jobs
    - allow in-flight jobs to finish until deadline
    """
    _draining.set()
    with _deadline_lock:
        global _deadline_at
        if _deadline_at is None:
            _deadline_at = time.time() + int(timeout_sec)
            logger.warning("drain_begin", timeout_sec=int(timeout_sec), deadline_at=_deadline_at)
    return drain_state()


def retry_after_seconds(default: int = 60) -> int:
    st = drain_state()
    if not st.draining:
        return 0
    if st.remaining_sec is None:
        return int(default)
    return max(1, int(st.remaining_sec))


def end_draining() -> None:
    """
    Exit draining mode (primarily for tests/dev).
    """
    _draining.clear()
    with _deadline_lock:
        global _deadline_at
        _deadline_at = None


@dataclass(slots=True)
class _LifecycleTasks:
    started: bool = False
    task_group: Any | None = None
    tasks: list[asyncio.Task] = field(default_factory=list)

    async def open(self) -> None:
        if self.task_group is not None:
            return
        if hasattr(asyncio, "TaskGroup"):
            tg = asyncio.TaskGroup()
            await tg.__aenter__()
            self.task_group = tg

    def create_task(self, coro, *, name: str) -> asyncio.Task:
        if self.task_group is not None:
            task = self.task_group.create_task(coro, name=name)
        else:
            task = asyncio.create_task(coro, name=name)
        self.tasks.append(task)
        return task

    async def stop(self) -> None:
        for t in list(self.tasks):
            t.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.task_group is not None:
            try:
                await self.task_group.__aexit__(None, None, None)
            except BaseException as ex:  # CancelledError may be wrapped in an ExceptionGroup (3.11+)
                eg_type = getattr(builtins, "BaseExceptionGroup", None)
                if isinstance(ex, asyncio.CancelledError):
                    pass
                elif eg_type is not None and isinstance(ex, eg_type):
                    only_cancelled = all(isinstance(e, asyncio.CancelledError) for e in ex.exceptions)
                    if not only_cancelled:
                        logger.warning("lifecycle_taskgroup_exit_failed", error=str(ex))
                else:
                    logger.warning("lifecycle_taskgroup_exit_failed", error=str(ex))
            self.task_group = None
        self.tasks.clear()


def _state(app_state: Any) -> _LifecycleTasks:
    st = getattr(app_state, "_lifecycle_tasks", None)
    if st is None:
        st = _LifecycleTasks()
        setattr(app_state, "_lifecycle_tasks", st)
    return st


async def _prune_loop(*, output_root: str, interval_s: float) -> None:
    try:
        while True:
            try:
                periodic_prune_tick(output_root=Path(output_root))
            except Exception as ex:
                logger.warning("workdir_prune_failed", error=str(ex))
            await asyncio.sleep(float(interval_s))
    except asyncio.CancelledError:
        logger.info("task stopped", task="workdir_prune")
        return


def _append_shutdown_logs(app_state: Any, message: str) -> None:
    store = getattr(app_state, "job_store", None)
    if store is None:
        return
    try:
        jobs = store.list(limit=200)
    except Exception:
        return
    for j in jobs:
        try:
            if j.state in {JobState.RUNNING, JobState.QUEUED}:
                store.append_log(j.id, f"[{now_utc()}] {message}")
        except Exception:
            continue


async def start_all(app_state: Any) -> None:
    st = _state(app_state)
    if st.started:
        return
    logger.info("startup begin", component="lifecycle")
    with suppress(Exception):
        end_draining()
    s = get_settings()

    queue_backend = getattr(app_state, "queue_backend", None)
    if queue_backend is not None:
        with suppress(Exception):
            await queue_backend.start()

    q = getattr(app_state, "job_queue", None)
    if q is not None:
        await q.start()

    out_root = getattr(app_state, "output_root", None) or s.output_dir
    interval = float(getattr(s, "work_prune_interval_sec", 300) or 300)
    await st.open()
    with suppress(Exception):
        periodic_prune_tick(output_root=Path(out_root))
    with suppress(Exception):
        st.create_task(
            _prune_loop(output_root=str(out_root), interval_s=interval),
            name="workdir.prune",
        )

    try:
        ModelManager.instance().prewarm()
    except Exception as ex:
        logger.warning("model_prewarm_exception", error=str(ex))

    st.started = True
    logger.info("startup end", component="lifecycle")


async def stop_all(app_state: Any) -> None:
    st = _state(app_state)
    if not st.started:
        return
    s = get_settings()
    logger.info("shutdown begin", component="lifecycle")
    begin_draining(timeout_sec=int(s.drain_timeout_sec))
    _append_shutdown_logs(app_state, "shutdown begin")

    queue_backend = getattr(app_state, "queue_backend", None)
    if queue_backend is not None:
        with suppress(asyncio.CancelledError, Exception):
            await queue_backend.stop()
        logger.info("queue stopped", queue="queue_backend")

    sched = getattr(app_state, "scheduler", None)
    if sched is not None:
        with suppress(Exception):
            sched.stop()
        logger.info("task stopped", task="scheduler")

    q = getattr(app_state, "job_queue", None)
    if q is not None:
        with suppress(asyncio.CancelledError, Exception):
            await q.graceful_shutdown(timeout_s=int(s.drain_timeout_sec))
        with suppress(asyncio.CancelledError, Exception):
            await q.stop()
        logger.info("queue stopped", queue="job_queue")
        _append_shutdown_logs(app_state, "queue stopped")

    await st.stop()

    with suppress(Exception):
        from dubbing_pipeline.web.routes_webrtc import shutdown_webrtc_peers

        await shutdown_webrtc_peers()

    _append_shutdown_logs(app_state, "shutdown end")
    st.started = False
    logger.info("shutdown end", component="lifecycle")
