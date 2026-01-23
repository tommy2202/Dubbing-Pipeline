from __future__ import annotations

import os

import pytest

from dubbing_pipeline.config import get_settings


@pytest.fixture(autouse=True)
def _test_env(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path_factory.mktemp("dp_test")
    (root / "Input").mkdir(parents=True, exist_ok=True)
    (root / "Output").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "_state").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APP_ROOT", str(root))
    monkeypatch.setenv("INPUT_DIR", str(root / "Input"))
    monkeypatch.setenv("DUBBING_OUTPUT_DIR", str(root / "Output"))
    monkeypatch.setenv("DUBBING_LOG_DIR", str(root / "logs"))
    monkeypatch.setenv("DUBBING_STATE_DIR", str(root / "_state"))
    monkeypatch.setenv("MIN_FREE_GB", "0")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("DUBBING_SKIP_STARTUP_CHECK", "1")
    get_settings.cache_clear()
