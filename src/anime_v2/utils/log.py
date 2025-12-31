from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _log_path() -> Path:
    # Requirement: logs/app.log
    # Use CWD-relative path so local dev "just works" from repo root.
    return Path(os.environ.get("ANIME_V2_LOG_DIR", "logs")) / "app.log"


def _configure_logger() -> logging.Logger:
    logger = logging.getLogger("anime_v2")
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

    # Avoid duplicate handlers if this module is imported repeatedly.
    if getattr(logger, "_anime_v2_configured", False):
        return logger

    log_path = _log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s [%(process)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        filename=str(log_path),
        maxBytes=int(os.environ.get("LOG_MAX_BYTES", str(10 * 1024 * 1024))),  # 10MB
        backupCount=int(os.environ.get("LOG_BACKUP_COUNT", "5")),
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logger.level)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    stream_handler.setLevel(logger.level)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False

    setattr(logger, "_anime_v2_configured", True)
    logger.debug("Logger initialized → %s", log_path)
    return logger


logger = _configure_logger()

# Convenience module-level functions (supports: `from anime_v2.utils import log; log.info("...")`)
debug = logger.debug
info = logger.info
warning = logger.warning
error = logger.error
exception = logger.exception

