from __future__ import annotations

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

