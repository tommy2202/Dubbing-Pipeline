from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Any

from anime_v2.config import get_settings
from anime_v2.gates.license import require_coqui_tos
from anime_v2.runtime.device_allocator import pick_device
from anime_v2.utils.log import logger
from anime_v2.utils.net import egress_guard


@dataclass
class _Entry:
    kind: str  # whisper|tts
    model_name: str
    device: str
    model: Any
    refcount: int = 0
    last_used: float = 0.0


class ModelManager:
    """
    Lazily loads and caches heavy ML models (Whisper, Coqui TTS) with:
    - per-(kind, model_name, device) caches
    - LRU eviction (best-effort, does not evict in-use entries)
    - thread safety
    """

    _singleton: ModelManager | None = None
    _singleton_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str, str], _Entry] = {}

    @classmethod
    def instance(cls) -> ModelManager:
        with cls._singleton_lock:
            if cls._singleton is None:
                cls._singleton = cls()
            return cls._singleton

    def _cache_max(self) -> int:
        s = get_settings()
        try:
            return max(1, int(s.model_cache_max))
        except Exception:
            return 3

    def _touch(self, e: _Entry) -> None:
        e.last_used = time.monotonic()

    def _evict_if_needed(self) -> None:
        max_n = self._cache_max()
        if len(self._cache) <= max_n:
            return
        # Evict least-recently-used entries with refcount==0
        victims = sorted(
            [e for e in self._cache.values() if int(e.refcount) <= 0],
            key=lambda x: x.last_used,
        )
        while len(self._cache) > max_n and victims:
            v = victims.pop(0)
            key = (v.kind, v.model_name, v.device)
            self._cache.pop(key, None)
            logger.info("model_evicted", kind=v.kind, model=v.model_name, device=v.device)

    def _load_whisper(self, model_name: str, device: str) -> Any:
        try:
            import whisper  # type: ignore
        except Exception as ex:
            raise RuntimeError(f"whisper not installed: {ex}") from ex
        # Respect global egress policy (OFFLINE_MODE etc.)
        with egress_guard():
            return whisper.load_model(model_name, device=device)

    def _load_tts(self, model_name: str, device: str) -> Any:
        require_coqui_tos()
        try:
            from TTS.api import TTS  # type: ignore
        except Exception as ex:
            raise RuntimeError(f"Coqui TTS not installed: {ex}") from ex
        # Coqui downloads models if missing; respect egress guard.
        with egress_guard():
            tts = TTS(model_name)
        # Best-effort: move to GPU if requested and supported.
        with suppress(Exception):
            # Some versions support `.to(device)`; others use internal torch modules.
            if device == "cuda" and hasattr(tts, "to"):
                tts.to("cuda")
        return tts

    def get_whisper(self, model_name: str, device: str) -> Any:
        key = ("whisper", str(model_name), str(device))
        with self._lock:
            e = self._cache.get(key)
            if e is not None:
                e.refcount += 1
                self._touch(e)
                return e.model
        # Load outside lock (expensive)
        model = self._load_whisper(str(model_name), str(device))
        with self._lock:
            e = self._cache.get(key)
            if e is not None:
                # Someone raced and loaded it; prefer cached
                e.refcount += 1
                self._touch(e)
                return e.model
            e = _Entry(
                kind="whisper",
                model_name=str(model_name),
                device=str(device),
                model=model,
                refcount=1,
                last_used=time.monotonic(),
            )
            self._cache[key] = e
            self._evict_if_needed()
            logger.info("model_loaded", kind="whisper", model=str(model_name), device=str(device))
            return model

    def get_tts(self, model_name: str, device: str) -> Any:
        key = ("tts", str(model_name), str(device))
        with self._lock:
            e = self._cache.get(key)
            if e is not None:
                e.refcount += 1
                self._touch(e)
                return e.model
        model = self._load_tts(str(model_name), str(device))
        with self._lock:
            e = self._cache.get(key)
            if e is not None:
                e.refcount += 1
                self._touch(e)
                return e.model
            e = _Entry(
                kind="tts",
                model_name=str(model_name),
                device=str(device),
                model=model,
                refcount=1,
                last_used=time.monotonic(),
            )
            self._cache[key] = e
            self._evict_if_needed()
            logger.info("model_loaded", kind="tts", model=str(model_name), device=str(device))
            return model

    def release(self, kind: str, model_name: str, device: str) -> None:
        key = (str(kind), str(model_name), str(device))
        with self._lock:
            e = self._cache.get(key)
            if e is None:
                return
            e.refcount = max(0, int(e.refcount) - 1)
            self._touch(e)
            self._evict_if_needed()

    @contextmanager
    def acquire_whisper(self, model_name: str, device: str) -> Iterator[Any]:
        m = self.get_whisper(model_name, device)
        try:
            yield m
        finally:
            self.release("whisper", model_name, device)

    @contextmanager
    def acquire_tts(self, model_name: str, device: str) -> Iterator[Any]:
        m = self.get_tts(model_name, device)
        try:
            yield m
        finally:
            self.release("tts", model_name, device)

    def prewarm(self) -> None:
        s = get_settings()
        whisper_list = [x.strip() for x in (s.prewarm_whisper or "").split(",") if x.strip()]
        tts_list = [x.strip() for x in (s.prewarm_tts or "").split(",") if x.strip()]
        if not whisper_list and not tts_list:
            return

        # Choose device for prewarm. Conservative allocator may choose cpu if telemetry missing.
        dev = pick_device("auto")
        logger.info("model_prewarm_start", device=dev, whisper=whisper_list, tts=tts_list)

        for m in whisper_list:
            try:
                with self.acquire_whisper(m, dev):
                    pass
            except Exception as ex:
                logger.warning(
                    "model_prewarm_failed", kind="whisper", model=m, device=dev, error=str(ex)
                )

        for m in tts_list:
            try:
                with self.acquire_tts(m, dev):
                    pass
            except Exception as ex:
                logger.warning(
                    "model_prewarm_failed", kind="tts", model=m, device=dev, error=str(ex)
                )

        logger.info("model_prewarm_done", device=dev)

    def state(self) -> list[dict[str, Any]]:
        """
        Return current in-process cache entries (safe for UI).
        """
        import time as _time

        now = _time.monotonic()
        with self._lock:
            items = list(self._cache.values())
        out: list[dict[str, Any]] = []
        for e in items:
            try:
                out.append(
                    {
                        "kind": str(e.kind),
                        "model_name": str(e.model_name),
                        "device": str(e.device),
                        "refcount": int(e.refcount),
                        "last_used_s": float(max(0.0, now - float(e.last_used or 0.0))),
                    }
                )
            except Exception:
                continue
        out.sort(key=lambda x: (x.get("kind", ""), x.get("model_name", ""), x.get("device", "")))
        return out
