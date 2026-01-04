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
    Copy into place via temp file + atomic replace.
    """
    ensure_dir(dst.parent)
    tmp = dst.with_suffix(dst.suffix + f".tmp.{os.getpid()}")
    logger.debug("Copying %s -> %s (tmp=%s)", src, dst, tmp)
    shutil.copy2(src, tmp)
    tmp.replace(dst)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding=encoding)
    tmp.replace(path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_bytes(data)
    tmp.replace(path)


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
    atomic_write_text(path, json.dumps(data, indent=indent, sort_keys=True), encoding="utf-8")
    logger.debug("Wrote JSON → %s", path)
