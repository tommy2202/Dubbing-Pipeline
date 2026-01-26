from __future__ import annotations

import os

from dubbing_pipeline.config import get_settings


def redis_available() -> bool:
    url = os.environ.get("REDIS_URL") or str(get_settings().redis_url or "")
    if not url:
        return False
    try:
        import redis  # type: ignore

        client = redis.Redis.from_url(url, decode_responses=True)
        return bool(client.ping())
    except Exception:
        return False


def redis_client():
    url = os.environ.get("REDIS_URL") or str(get_settings().redis_url or "")
    if not url:
        return None
    try:
        import redis  # type: ignore

        return redis.Redis.from_url(url, decode_responses=True)
    except Exception:
        return None


def redis_prefix() -> str:
    return str(get_settings().redis_queue_prefix or "dp").strip().strip(":") or "dp"
