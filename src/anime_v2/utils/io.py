from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .log import logger


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_copy(src: Path, dst: Path) -> None:
    """
    Simple helper for copying files into place.
    (Placeholder: upgrade to atomic rename with temp files if needed.)
    """
    ensure_dir(dst.parent)
    logger.debug("Copying %s -> %s", src, dst)
    shutil.copy2(src, dst)


def read_json(path: Path, *, default: object | None = None) -> object:
    """
    Read JSON with a default fallback.
    Returns `default` if file does not exist.
    """
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: object, *, indent: int = 2) -> None:
    """
    Write JSON safely via atomic replace.
    """
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=indent, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    logger.debug("Wrote JSON → %s", path)
