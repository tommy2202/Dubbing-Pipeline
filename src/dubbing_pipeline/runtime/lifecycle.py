from __future__ import annotations

import asyncio
import builtins
import os
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import JobState, now_utc
from dubbing_pipeline.ops.storage import ensure_free_space, periodic_prune_tick
from dubbing_pipeline.runtime.model_manager import ModelManager
from dubbing_pipeline.utils.log import logger
from dubbing_pipeline.utils.paths import default_paths

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


def _run_version(cmd: list[str]) -> str | None:
    try:
        p = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    return out.splitlines()[0] if out else (err.splitlines()[0] if err else None)


def _ensure_writable_dir(path: Path, *, label: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not path.exists() or not path.is_dir():
        raise RuntimeError(f"{label} is not a directory: {path}")
    if not os.access(str(path), os.W_OK):
        raise RuntimeError(f"{label} is not writable: {path}")
    test_path = path / ".write_check"
    try:
        test_path.write_text("ok\n", encoding="utf-8")
    finally:
        with suppress(Exception):
            test_path.unlink()


def _startup_self_check(app_state: Any) -> None:
    s = get_settings()
    logger.info("startup_self_check_begin")

    ffmpeg_v = _run_version([str(s.ffmpeg_bin), "-version"])
    if not ffmpeg_v:
        logger.error("startup_self_check_failed", check="ffmpeg", hint="install ffmpeg")
        raise RuntimeError("ffmpeg not found or not runnable")
    logger.info("startup_check_ok", check="ffmpeg", version=ffmpeg_v)

    ffprobe_v = _run_version([str(s.ffprobe_bin), "-version"])
    if not ffprobe_v:
        logger.error("startup_self_check_failed", check="ffprobe", hint="install ffprobe")
        raise RuntimeError("ffprobe not found or not runnable")
    logger.info("startup_check_ok", check="ffprobe", version=ffprobe_v)

    paths = default_paths()
    out_root = Path(s.output_dir).resolve()
    input_root = (
        Path(str(s.input_dir)).resolve()
        if getattr(s, "input_dir", None)
        else (Path(s.app_root).resolve() / "Input")
    )
    uploads_root = Path(paths.uploads_dir).resolve()
    _ensure_writable_dir(out_root, label="Output dir")
    _ensure_writable_dir(input_root, label="Input dir")
    _ensure_writable_dir(uploads_root, label="Uploads dir")
    logger.info(
        "startup_check_ok",
        check="paths",
        output_dir=str(out_root),
        input_dir=str(input_root),
        uploads_dir=str(uploads_root),
    )

    min_free_gb = int(getattr(s, "min_free_gb", 0) or 0)
    if min_free_gb > 0:
        try:
            ensure_free_space(min_gb=min_free_gb, path=out_root)
            logger.info("startup_check_ok", check="disk_free", min_free_gb=int(min_free_gb))
        except Exception as ex:
            logger.error("startup_self_check_failed", check="disk_free", error=str(ex))
            raise RuntimeError(f"insufficient free disk space: {ex}") from ex
    else:
        logger.info("startup_check_ok", check="disk_free", min_free_gb=0, note="check disabled")

    max_upload_mb = int(getattr(s, "max_upload_mb", 0) or 0)
    upload_chunk_bytes = int(getattr(s, "upload_chunk_bytes", 0) or 0)
    if max_upload_mb <= 0:
        logger.warning("startup_self_check_warn", check="upload_caps", max_upload_mb=max_upload_mb)
    else:
        logger.info(
            "startup_check_ok",
            check="upload_caps",
            max_upload_mb=max_upload_mb,
            upload_chunk_bytes=upload_chunk_bytes,
        )

    quotas = {
        "max_active_jobs_per_user": int(getattr(s, "max_active_jobs_per_user", 0) or 0),
        "max_queued_jobs_per_user": int(getattr(s, "max_queued_jobs_per_user", 0) or 0),
        "daily_job_cap": int(getattr(s, "daily_job_cap", 0) or 0),
        "max_concurrent_per_user": int(getattr(s, "max_concurrent_per_user", 0) or 0),
    }
    if any(v < 0 for v in quotas.values()):
        logger.warning("startup_self_check_warn", check="quotas", quotas=quotas)
    else:
        logger.info("startup_check_ok", check="quotas", quotas=quotas)

    logger.info("startup_self_check_end")


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

    _startup_self_check(app_state)

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
