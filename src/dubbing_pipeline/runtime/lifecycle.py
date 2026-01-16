from __future__ import annotations

import threading
import time
from dataclasses import dataclass

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
