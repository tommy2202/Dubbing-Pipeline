from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.log import logger

_lock = threading.Lock()


def _cache_root() -> Path:
    # Must be writable (container rootfs is read-only); default under Output/
    s = get_settings()
    base = Path(s.cache_dir or (Path(s.output_dir) / "cache"))
    base.mkdir(parents=True, exist_ok=True)
    return base.resolve()


def _index_path() -> Path:
    return _cache_root() / "index.json"


def _read_index() -> dict[str, Any]:
    p = _index_path()
    if not p.exists():
        return {"version": 1, "created_at": time.time(), "items": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            return data
    except Exception:
        pass
    return {"version": 1, "created_at": time.time(), "items": {}}


def _write_index(data: dict[str, Any]) -> None:
    p = _index_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def make_key(namespace: str, parts: dict[str, Any]) -> str:
    blob = json.dumps(
        {"ns": namespace, "parts": parts}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"{namespace}:{hashlib.sha256(blob).hexdigest()}"


def cache_get(key: str) -> dict[str, Any] | None:
    with _lock:
        idx = _read_index()
        item = idx.get("items", {}).get(key)
        if not isinstance(item, dict):
            return None
        # Validate paths exist
        paths = item.get("paths", {})
        if not isinstance(paths, dict) or not paths:
            return None
        for _, p in paths.items():
            try:
                if not Path(str(p)).exists():
                    return None
            except Exception:
                return None
        return item


def cache_put(
    key: str, paths: dict[str, str | Path], *, meta: dict[str, Any] | None = None
) -> None:
    with _lock:
        idx = _read_index()
        items = idx.setdefault("items", {})
        if not isinstance(items, dict):
            items = {}
            idx["items"] = items
        items[key] = {
            "paths": {k: str(v) for k, v in paths.items()},
            "meta": meta or {},
            "created_at": time.time(),
        }
        _write_index(idx)
        logger.info("cache_put", key=key, paths=list(paths.keys()))
