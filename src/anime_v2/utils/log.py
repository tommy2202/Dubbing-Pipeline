from __future__ import annotations

import logging
import os
import re
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

    class RedactingFormatter(logging.Formatter):
        # Redact common secret/token patterns in the fully formatted output (incl. tracebacks).
        _jwt_re = re.compile(r"\beyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\b")
        _api_key_re = re.compile(r"\bdp_[a-z0-9]{6,}_[A-Za-z0-9_\-]{10,}\b", re.IGNORECASE)
        _bearer_re = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9_\-\.=]+)")
        _kv_re = re.compile(
            r"(?i)\b(jwt_secret|csrf_secret|session_secret|huggingface_token|hf_token|token|secret|password|api_key)\b\s*=\s*([^\s,;]+)"
        )

        def format(self, record: logging.LogRecord) -> str:
            s = super().format(record)
            s = self._jwt_re.sub("***REDACTED***", s)
            s = self._api_key_re.sub("***REDACTED***", s)
            s = self._bearer_re.sub("Bearer ***REDACTED***", s)
            s = self._kv_re.sub(lambda m: f"{m.group(1)}=***REDACTED***", s)
            return s

    fmt = RedactingFormatter(fmt="%(asctime)s %(levelname)s %(name)s [%(process)d] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = RotatingFileHandler(
        filename=str(log_path),
        maxBytes=int(os.environ.get("LOG_MAX_BYTES", str(5 * 1024 * 1024))),  # 5MB
        backupCount=int(os.environ.get("LOG_BACKUP_COUNT", "3")),
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

