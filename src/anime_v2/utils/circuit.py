from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

from anime_v2.config import get_settings


@dataclass
class CircuitState:
    state: str  # closed|open|half_open
    failures: int
    opened_at: float
    half_open_inflight: bool


class Circuit:
    """
    Simple in-proc circuit breaker.

    - closed: allow
    - open: deny until cooldown elapsed
    - half_open: allow one trial call; success closes, failure re-opens
    """

    _registry: dict[str, Circuit] = {}
    _reg_lock = threading.Lock()

    def __init__(self, name: str) -> None:
        self.name = str(name)
        self._lock = threading.Lock()
        self._failures = 0
        self._state = "closed"
        self._opened_at = 0.0
        self._half_open_inflight = False

    @classmethod
    def get(cls, name: str) -> Circuit:
        with cls._reg_lock:
            c = cls._registry.get(name)
            if c is None:
                c = cls(name)
                cls._registry[name] = c
            return c

    def _threshold(self) -> int:
        s = get_settings()
        try:
            return int(s.cb_fail_threshold)
        except Exception:
            return int(os.environ.get("CB_FAIL_THRESHOLD", "5"))

    def _cooldown(self) -> float:
        s = get_settings()
        try:
            return float(s.cb_cooldown_sec)
        except Exception:
            return float(os.environ.get("CB_COOLDOWN_SEC", "60"))

    def snapshot(self) -> CircuitState:
        with self._lock:
            return CircuitState(
                self._state,
                int(self._failures),
                float(self._opened_at),
                bool(self._half_open_inflight),
            )

    def allow(self) -> bool:
        with self._lock:
            if self._state == "closed":
                return True
            now = time.monotonic()
            if self._state == "open":
                if (now - self._opened_at) >= self._cooldown():
                    self._state = "half_open"
                    self._half_open_inflight = False
                else:
                    return False
            if self._state == "half_open":
                if self._half_open_inflight:
                    return False
                self._half_open_inflight = True
                return True
            return True

    def mark_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = "closed"
            self._opened_at = 0.0
            self._half_open_inflight = False

    def mark_failure(self) -> None:
        with self._lock:
            self._failures += 1
            thresh = self._threshold()
            if self._state == "half_open":
                # trial failed -> open immediately
                self._state = "open"
                self._opened_at = time.monotonic()
                self._half_open_inflight = False
                return
            if self._failures >= thresh:
                self._state = "open"
                self._opened_at = time.monotonic()
                self._half_open_inflight = False
            else:
                # allow continued attempts
                if self._state == "open":
                    self._state = "closed"
