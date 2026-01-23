from __future__ import annotations

from typing import Any, Callable

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.runtime.scheduler import Scheduler
from dubbing_pipeline.utils.log import logger

from .fallback_local_queue import FallbackLocalQueue as LocalQueue
from .manager import AutoQueueBackend
from .redis_queue import RedisQueue

__all__ = ["LocalQueue", "RedisQueue", "build_queue_backend"]


def build_queue_backend(
    *,
    scheduler: Scheduler,
    get_store_cb: Callable[[], Any],
    enqueue_job_id_cb: Callable[[str], "Any"],
) -> AutoQueueBackend:
    """
    Return the canonical queue backend with optional scale-path override.

    QUEUE_BACKEND:
      - local  -> force fallback queue
      - redis  -> require Redis queue
      - unset  -> keep existing auto behavior
    """
    s = get_settings()
    backend = str(getattr(s, "queue_backend", "") or "").strip().lower()
    mode_override = None
    if backend:
        if backend in {"local", "fallback"}:
            mode_override = "fallback"
        elif backend == "redis":
            mode_override = "redis"
        else:
            logger.warning("queue_backend_invalid", value=str(backend))
    return AutoQueueBackend(
        scheduler=scheduler,
        get_store_cb=get_store_cb,
        enqueue_job_id_cb=enqueue_job_id_cb,
        mode_override=mode_override,
    )
