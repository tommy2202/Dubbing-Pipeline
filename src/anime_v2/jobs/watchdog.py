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
        # Optional: memory cap for watchdog child processes (best-effort; Linux only).
        try:
            from anime_v2.config import get_settings

            max_mb = int(getattr(get_settings(), "watchdog_child_max_mem_mb", 0) or 0)
        except Exception:
            max_mb = 0
        if max_mb > 0:
            try:
                import resource  # type: ignore

                limit = int(max_mb) * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            except Exception:
                # Non-fatal: continue without memory cap.
                pass
        v = fn(*args, **kwargs)
        q.put(PhaseResult(ok=True, value=v))
    except BaseException:
        q.put(PhaseResult(ok=False, error=traceback.format_exc()))


def run_with_timeout(
    name: str,
    *,
    timeout_s: int,
    fn: Callable,
    args: tuple = (),
    kwargs: dict | None = None,
    cancel_check: Callable[[], bool] | None = None,
    cancel_exc: BaseException | None = None,
) -> Any:
    """
    Run a blocking phase in a separate process so we can SIGKILL on timeout.
    """
    kwargs = kwargs or {}
    q: mp.Queue = mp.Queue(maxsize=1)
    p = mp.Process(target=_child_main, args=(q, fn, args, kwargs), daemon=True)
    p.start()
    deadline = __import__("time").monotonic() + float(timeout_s)

    # Poll join so we can support cooperative cancellation (kill child early).
    while True:
        p.join(timeout=0.25)
        if not p.is_alive():
            break
        if cancel_check is not None:
            cancel_requested = False
            try:
                cancel_requested = bool(cancel_check())
            except Exception:
                cancel_requested = False
            if cancel_requested:
                # Cancel requested: terminate quickly.
                with suppress(Exception):
                    p.terminate()
                p.join(timeout=2.0)
                if p.is_alive():
                    with suppress(Exception):
                        os.kill(p.pid, signal.SIGKILL)  # type: ignore[arg-type]
                    p.join(timeout=2.0)
                if cancel_exc is not None:
                    raise cancel_exc
                raise PhaseTimeout(f"Phase '{name}' canceled and was killed")
        if __import__("time").monotonic() >= deadline:
            # Timeout: kill.
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
