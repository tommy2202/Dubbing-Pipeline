from __future__ import annotations

import pytest

from dubbing_pipeline.runtime.device_allocator import GpuStatus, pick_device
from dubbing_pipeline.runtime.model_manager import ModelManager


def test_device_allocator_auto_falls_back_when_saturated(monkeypatch: pytest.MonkeyPatch) -> None:
    import dubbing_pipeline.runtime.device_allocator as da

    monkeypatch.setattr(da, "_cuda_available", lambda: True)
    monkeypatch.setattr(da, "_read_gpu_status", lambda: GpuStatus(util_ratio=0.99, mem_ratio=0.95))
    monkeypatch.setenv("GPU_UTIL_MAX", "0.85")
    monkeypatch.setenv("GPU_MEM_MAX_RATIO", "0.90")
    from dubbing_pipeline.config import get_settings

    get_settings.cache_clear()
    assert pick_device("auto") == "cpu"


def test_model_manager_lru_eviction(monkeypatch: pytest.MonkeyPatch) -> None:
    mm = ModelManager()

    # Make loads cheap/deterministic
    monkeypatch.setattr(
        mm, "_load_whisper", lambda model_name, device: f"whisper:{model_name}:{device}"
    )
    monkeypatch.setattr(mm, "_load_tts", lambda model_name, device: f"tts:{model_name}:{device}")
    monkeypatch.setenv("MODEL_CACHE_MAX", "3")
    from dubbing_pipeline.config import get_settings

    get_settings.cache_clear()

    # Load 4 entries; with release, LRU can evict.
    with mm.acquire_whisper("a", "cpu"):
        pass
    with mm.acquire_whisper("b", "cpu"):
        pass
    with mm.acquire_tts("c", "cpu"):
        pass
    with mm.acquire_tts("d", "cpu"):
        pass

    # At most 3 entries should remain
    assert len(mm._cache) <= 3  # noqa: SLF001
