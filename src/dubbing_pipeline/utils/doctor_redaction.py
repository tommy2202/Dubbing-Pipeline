from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "***REDACTED***"

_JWT_RE = re.compile(r"\beyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\b")
_API_KEY_RE = re.compile(r"\bdp_[a-z0-9]{6,}_[A-Za-z0-9_\-]{10,}\b", re.IGNORECASE)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9_\-\.=]+)")
_BASIC_RE = re.compile(r"(?i)\bBasic\s+([A-Za-z0-9_\-+/=]+)")

_GH_TOKEN_RE = re.compile(r"\bgh[opsru]_[A-Za-z0-9]{30,}\b")
_SLACK_TOKEN_RE = re.compile(r"\bxox[aboprs]-\d+-\d+-\d+-[A-Za-z0-9-]+\b")
_OPENAI_RE = re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")
_GOOGLE_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")

_KV_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*?(?:token|secret|password|key|auth|apikey|api_key))\b(\s*[:=]\s*)([^\s,;]+)"
)
_HEADER_RE = re.compile(
    r"(?i)\b(authorization|x-api-key|x-auth-token|api-key)\b(\s*[:=]\s*)(?!Bearer\b)(?!Basic\b)([^\s,;]+)"
)
_URL_PARAM_RE = re.compile(
    r"(?i)([?&])(token|access_token|api_key|apikey|secret|password)=([^&\s]+)"
)


def redact(text: str) -> str:
    """
    Redact tokens/secrets from free text.
    """
    s = "" if text is None else str(text)
    s = _JWT_RE.sub(REDACTED, s)
    s = _API_KEY_RE.sub(REDACTED, s)
    s = _BEARER_RE.sub("Bearer " + REDACTED, s)
    s = _BASIC_RE.sub("Basic " + REDACTED, s)
    s = _GH_TOKEN_RE.sub(REDACTED, s)
    s = _SLACK_TOKEN_RE.sub(REDACTED, s)
    s = _OPENAI_RE.sub(REDACTED, s)
    s = _GOOGLE_API_KEY_RE.sub(REDACTED, s)
    s = _KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", s)
    s = _HEADER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", s)
    s = _URL_PARAM_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}={REDACTED}", s)
    return s


def redact_obj(value: Any) -> Any:
    """
    Recursively redact strings within nested objects.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, Mapping):
        return {str(k): redact_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact_obj(v) for v in value]
    return value
