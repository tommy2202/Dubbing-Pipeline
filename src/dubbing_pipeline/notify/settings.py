from __future__ import annotations

import re
from typing import Iterable

from dubbing_pipeline.config import get_settings

_TOPIC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def normalize_topic(topic: str | None) -> str:
    return str(topic or "").strip()


def is_valid_topic(topic: str) -> bool:
    if not topic:
        return False
    if "/" in topic or "\\" in topic:
        return False
    return bool(_TOPIC_RE.match(topic))


def parse_allowed_topics(raw: str | None) -> list[str]:
    if not raw:
        return []
    items = re.split(r"[,\s]+", str(raw).strip())
    out: list[str] = []
    for it in items:
        t = normalize_topic(it)
        if not t:
            continue
        if not is_valid_topic(t):
            continue
        if t not in out:
            out.append(t)
    return out


def allowed_topics() -> list[str]:
    s = get_settings()
    raw = str(getattr(s, "ntfy_allowed_topics", "") or "")
    return parse_allowed_topics(raw)


def validate_topic(topic: str | None, allowed: Iterable[str] | None = None) -> str:
    t = normalize_topic(topic)
    if not t:
        return ""
    if not is_valid_topic(t):
        raise ValueError("invalid_topic")
    allow = list(allowed or [])
    if allow and t not in allow:
        raise ValueError("topic_not_allowed")
    return t
