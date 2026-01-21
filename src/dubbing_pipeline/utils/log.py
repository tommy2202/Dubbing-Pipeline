from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping
from contextlib import suppress
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

from dubbing_pipeline.config import get_settings

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)


def set_request_id(rid: str | None) -> None:
    request_id_var.set(rid)


def set_user_id(uid: str | None) -> None:
    user_id_var.set(uid)


def _log_path() -> Path:
    s = get_settings()
    return Path(s.log_dir) / "app.log"


_JWT_RE = re.compile(r"\beyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\b")
_API_KEY_RE = re.compile(r"\bdp_[a-z0-9]{6,}_[A-Za-z0-9_\-]{10,}\b", re.IGNORECASE)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9_\-\.=]+)")
_BASIC_RE = re.compile(r"(?i)\bBasic\s+([A-Za-z0-9_\-+/=]+)")
_COOKIE_RE = re.compile(
    r"(?i)\b(session|refresh|csrf|access_token|refresh_token|token|auth)=([^;,\s]+)"
)
_HEADER_RE = re.compile(
    r"(?i)\b(authorization|cookie|set-cookie|x-api-key|x-csrf-token)\b\s*[:=]\s*([^\n]+)"
)
_TEXT_INLINE_RE = re.compile(
    r"(?i)\b(transcript|subtitle|subtitles|content|prompt)\b\s*[:=]\s*([^\n]{20,})"
)
_KV_RE = re.compile(
    r"(?i)\b(jwt_secret|csrf_secret|session_secret|huggingface_token|hf_token|token|secret|password|api_key|authorization|cookie)\b\s*=\s*([^\s,;]+)"
)

_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-csrf-token",
    "csrf",
    "csrf_token",
    "session",
    "refresh",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "password",
    "secret",
    "jwt",
}
_TEXT_KEYS = {"text", "subtitle", "subtitles", "transcript", "content", "prompt"}
_LIST_KEYS = {"segments", "lines", "items", "updates"}


def _secret_literals() -> list[str]:
    """
    Return configured secret values that must never appear in logs.
    Best-effort (safe even if settings aren't fully initialized yet).
    """
    vals: list[str] = []
    try:
        s = get_settings()
        sec = getattr(s, "secret", None)
        if sec is not None:
            # Best-effort: include all secret-config values.
            field_names = getattr(sec.__class__, "model_fields", {}) or {}
            for name in field_names.keys():
                with suppress(Exception):
                    v = getattr(sec, name)
                raw = ""
                if v is None:
                    continue
                if hasattr(v, "get_secret_value"):
                    with suppress(Exception):
                        raw = str(v.get_secret_value() or "")
                elif isinstance(v, str):
                    raw = str(v or "")
                if raw:
                    vals.append(raw)
    except Exception:
        pass

    # De-dupe and ignore tiny values to avoid over-redaction.
    out: list[str] = []
    for v in vals:
        v = str(v)
        if len(v) < 8:
            continue
        if v not in out:
            out.append(v)
    return out


def _redact_str(s: str) -> str:
    # Exact-value replacement first (covers non-token secrets like passwords)
    with suppress(Exception):
        for lit in _secret_literals():
            if lit and lit in s:
                s = s.replace(lit, "***REDACTED***")
    s = _JWT_RE.sub("***REDACTED***", s)
    s = _API_KEY_RE.sub("***REDACTED***", s)
    s = _BEARER_RE.sub("Bearer ***REDACTED***", s)
    s = _BASIC_RE.sub("Basic ***REDACTED***", s)
    s = _COOKIE_RE.sub(lambda m: f"{m.group(1)}=***REDACTED***", s)
    s = _HEADER_RE.sub(lambda m: f"{m.group(1)}: ***REDACTED***", s)
    s = _TEXT_INLINE_RE.sub(lambda m: f"{m.group(1)}=***REDACTED***", s)
    s = _KV_RE.sub(lambda m: f"{m.group(1)}=***REDACTED***", s)
    return s


def _is_sensitive_key(key: str | None) -> bool:
    if not key:
        return False
    k = str(key).strip().lower()
    if k in _SENSITIVE_KEYS:
        return True
    return any(k.endswith(s) for s in ("_secret", "_token", "_password", "_key"))


def _scrub_obj(obj: Any, *, key: str | None = None) -> Any:
    if _is_sensitive_key(key):
        return "***REDACTED***"
    if isinstance(obj, str):
        if key and str(key).strip().lower() in _TEXT_KEYS:
            return {"redacted": True, "len": len(obj)}
        return _redact_str(obj)
    if isinstance(obj, bytes):
        return {"bytes": len(obj)}
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            kk = str(k)
            if kk.strip().lower() in _LIST_KEYS and isinstance(v, list):
                out[kk] = {"count": len(v)}
            else:
                out[kk] = _scrub_obj(v, key=kk)
        return out
    if isinstance(obj, (list, tuple, set)):
        return [_scrub_obj(v) for v in obj]
    return obj


def safe_log_data(data: Any) -> Any:
    return _scrub_obj(data)


def safe_log(event: str, **kwargs: Any) -> None:
    logger.info(event, **safe_log_data(kwargs))


def redact_event(_, __, event_dict: dict[str, Any]) -> dict[str, Any]:
    return _scrub_obj(event_dict)


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
    s = get_settings()
    level = str(s.log_level).upper()
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
    if getattr(root, "_dubbing_pipeline_structlog_configured", False):
        return structlog.get_logger("dubbing_pipeline")

    foreign_pre_chain = [
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        structlog.stdlib.add_log_level,
        add_contextvars,
        structlog.processors.format_exc_info,
        redact_event,
        rename_event_to_msg,
    ]

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=foreign_pre_chain,
    )

    file_handler = RotatingFileHandler(
        filename=str(log_path),
        maxBytes=int(s.log_max_bytes),
        backupCount=int(s.log_backup_count),
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
            structlog.processors.format_exc_info,
            redact_event,
            rename_event_to_msg,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root._dubbing_pipeline_structlog_configured = True
    return structlog.get_logger("dubbing_pipeline")


logger = _configure_structlog()

# Backwards-compatible helpers
debug = logger.debug
info = logger.info
warning = logger.warning
error = logger.error
exception = logger.exception


def set_log_level(level: str) -> None:
    """
    Best-effort runtime log level override (CLI convenience).
    Does not change handlers/formatters; only raises/lowers filtering level.
    """
    try:
        lvl = getattr(logging, str(level).upper(), logging.INFO)
        root = logging.getLogger()
        root.setLevel(lvl)
        for h in root.handlers:
            with suppress(Exception):
                h.setLevel(lvl)
    except Exception:
        # keep existing configuration
        return
