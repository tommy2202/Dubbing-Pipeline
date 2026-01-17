from __future__ import annotations

import os

import pytest

from config.settings import ConfigError, get_settings


def test_production_blocks_weak_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "production")
    # Defaults are weak; production must fail fast.
    get_settings.cache_clear()
    with pytest.raises(ConfigError):
        _ = get_settings()
