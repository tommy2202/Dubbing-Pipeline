from __future__ import annotations

import logging
import os
import re
import sys
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)


def set_request_id(rid: str | None) -> None:
    request_id_var.set(rid)


def set_user_id(uid: str | None) -> None:
    user_id_var.set(uid)


def _log_path() -> Path:
    return Path(os.environ.get("ANIME_V2_LOG_DIR", "logs")) / "app.log"


_JWT_RE = re.compile(r"\beyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\b")
_API_KEY_RE = re.compile(r"\bdp_[a-z0-9]{6,}_[A-Za-z0-9_\-]{10,}\b", re.IGNORECASE)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9_\-\.=]+)")
_KV_RE = re.compile(
    r"(?i)\b(jwt_secret|csrf_secret|session_secret|huggingface_token|hf_token|token|secret|password|api_key)\b\s*=\s*([^\s,;]+)"
)


def _redact_str(s: str) -> str:
    s = _JWT_RE.sub("***REDACTED***", s)
    s = _API_KEY_RE.sub("***REDACTED***", s)
    s = _BEARER_RE.sub("Bearer ***REDACTED***", s)
    s = _KV_RE.sub(lambda m: f"{m.group(1)}=***REDACTED***", s)
    return s


def redact_event(_, __, event_dict: dict[str, Any]) -> dict[str, Any]:
    for k, v in list(event_dict.items()):
        if isinstance(v, str):
            event_dict[k] = _redact_str(v)
    return event_dict


def add_contextvars(_, __, event_dict: dict[str, Any]) -> dict[str, Any]:
    rid = request_id_var.get()
    uid = user_id_var.get()
    if rid:
        event_dict.setdefault("request_id", rid)
    if uid:
        event_dict.setdefault("user_id", uid)
    return event_dict


def rename_event_to_msg(_, __, event_dict: dict[str, Any]) -> dict[str, Any]:
    if "msg" not in event_dict and "event" in event_dict:
        event_dict["msg"] = event_dict.pop("event")
    return event_dict


def _configure_structlog() -> structlog.stdlib.BoundLogger:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_path = _log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # If an older plaintext app.log exists, move it aside so the current file is JSON-only.
    try:
        if log_path.exists() and log_path.is_file() and log_path.stat().st_size > 0:
            with log_path.open("rb") as f:
                first = f.read(1)
            if first and first != b"{":
                legacy = log_path.with_name(
                    f"app.log.legacy-{__import__('datetime').datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
                )
                log_path.replace(legacy)
    except Exception:
        pass

    root = logging.getLogger()
    root.setLevel(level)

    # Avoid duplicates if re-imported
    if getattr(root, "_anime_v2_structlog_configured", False):
        return structlog.get_logger("anime_v2")

    foreign_pre_chain = [
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        structlog.stdlib.add_log_level,
        add_contextvars,
        redact_event,
        structlog.processors.format_exc_info,
        rename_event_to_msg,
    ]

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=foreign_pre_chain,
    )

    file_handler = RotatingFileHandler(
        filename=str(log_path),
        maxBytes=int(os.environ.get("LOG_MAX_BYTES", str(5 * 1024 * 1024))),
        backupCount=int(os.environ.get("LOG_BACKUP_COUNT", "3")),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            structlog.stdlib.add_log_level,
            add_contextvars,
            redact_event,
            structlog.processors.format_exc_info,
            rename_event_to_msg,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root._anime_v2_structlog_configured = True
    return structlog.get_logger("anime_v2")


logger = _configure_structlog()

# Backwards-compatible helpers
debug = logger.debug
info = logger.info
warning = logger.warning
error = logger.error
exception = logger.exception
