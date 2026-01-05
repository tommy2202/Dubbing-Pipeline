from __future__ import annotations

import time

import pytest

from anime_v2.utils.circuit import Circuit
from anime_v2.utils.retry import retry_call


def test_retry_call_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("fail")
        return "ok"

    # don't actually sleep in tests
    monkeypatch.setattr(time, "sleep", lambda _: None)
    assert retry_call(fn, retries=5, base=0.001, cap=0.01, jitter=False) == "ok"
    assert calls["n"] == 3


def test_circuit_opens_and_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    from anime_v2.config import get_settings

    monkeypatch.setenv("CB_FAIL_THRESHOLD", "2")
    monkeypatch.setenv("CB_COOLDOWN_SEC", "1")
    get_settings.cache_clear()

    c = Circuit.get("unit")
    c.mark_success()
    assert c.allow() is True
    c.mark_failure()
    assert c.allow() is True
    c.mark_failure()
    # should be open now
    assert c.allow() is False
