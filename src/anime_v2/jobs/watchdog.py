from __future__ import annotations

import multiprocessing as mp
import os
import signal
import traceback
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any


class PhaseTimeout(TimeoutError):
    pass


@dataclass(frozen=True, slots=True)
class PhaseResult:
    ok: bool
    value: Any = None
    error: str | None = None


def _child_main(q: mp.Queue, fn: Callable, args: tuple, kwargs: dict) -> None:
    try:
        v = fn(*args, **kwargs)
        q.put(PhaseResult(ok=True, value=v))
    except BaseException:
        q.put(PhaseResult(ok=False, error=traceback.format_exc()))


def run_with_timeout(
    name: str, *, timeout_s: int, fn: Callable, args: tuple = (), kwargs: dict | None = None
) -> Any:
    """
    Run a blocking phase in a separate process so we can SIGKILL on timeout.
    """
    kwargs = kwargs or {}
    q: mp.Queue = mp.Queue(maxsize=1)
    p = mp.Process(target=_child_main, args=(q, fn, args, kwargs), daemon=True)
    p.start()
    p.join(timeout=float(timeout_s))

    if p.is_alive():
        # Try graceful terminate first, then SIGKILL
        with suppress(Exception):
            p.terminate()
        p.join(timeout=2.0)
        if p.is_alive():
            with suppress(Exception):
                os.kill(p.pid, signal.SIGKILL)  # type: ignore[arg-type]
            p.join(timeout=2.0)
        raise PhaseTimeout(f"Phase '{name}' exceeded timeout ({timeout_s}s) and was killed")

    try:
        res: PhaseResult = q.get_nowait()
    except Exception as ex:
        raise RuntimeError(f"Phase '{name}' failed without returning a result") from ex

    if not res.ok:
        raise RuntimeError(f"Phase '{name}' failed:\n{res.error}")
    return res.value
