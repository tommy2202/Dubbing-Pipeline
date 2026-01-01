from __future__ import annotations

import random
import time
from typing import Any, Callable


def retry_call(
    fn: Callable[[], Any],
    *,
    retries: int = 3,
    base: float = 0.5,
    cap: float = 8.0,
    jitter: bool = True,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
) -> Any:
    """
    Call fn() with capped exponential backoff (+ optional jitter).

    retries: number of retry attempts (so total calls = 1 + retries)
    """
    attempt = 0
    while True:
        try:
            return fn()
        except BaseException as ex:
            if attempt >= int(retries):
                raise
            delay = min(float(cap), float(base) * (2**attempt))
            if jitter:
                delay = delay * (0.5 + random.random())
            attempt += 1
            if on_retry is not None:
                try:
                    on_retry(attempt, delay, ex)
                except Exception:
                    pass
            time.sleep(max(0.0, float(delay)))

