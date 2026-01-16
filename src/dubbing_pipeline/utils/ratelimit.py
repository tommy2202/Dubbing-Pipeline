from __future__ import annotations

import time
from dataclasses import dataclass

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.log import logger


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class RateLimiter:
    """
    Simple rate limiter with Redis optional backend.
    Keys should include both scope and identity (e.g. "auth:ip:1.2.3.4").
    """

    def __init__(self) -> None:
        s = get_settings()
        self.redis_url = s.redis_url
        self._mem: dict[str, _Bucket] = {}

    def _redis(self):
        if not self.redis_url:
            return None
        try:
            import redis  # type: ignore

            return redis.Redis.from_url(self.redis_url, decode_responses=True)
        except Exception as ex:
            logger.warning("redis unavailable (%s); falling back to in-proc limiter", ex)
            return None

    def allow(self, key: str, *, limit: int, per_seconds: int) -> bool:
        now = time.time()
        rate = float(limit) / float(per_seconds)
        r = self._redis()
        if r is not None:
            # Token bucket in Redis (best-effort)
            # Store: tokens, ts
            try:
                pipe = r.pipeline()
                pipe.hmget(key, "tokens", "ts")
                tokens_s, ts_s = pipe.execute()[0]
                tokens = float(tokens_s) if tokens_s is not None else float(limit)
                ts = float(ts_s) if ts_s is not None else now
                tokens = min(float(limit), tokens + (now - ts) * rate)
                if tokens < 1.0:
                    # ensure key expires
                    r.expire(key, per_seconds * 2)
                    return False
                tokens -= 1.0
                r.hset(key, mapping={"tokens": f"{tokens:.6f}", "ts": f"{now:.6f}"})
                r.expire(key, per_seconds * 2)
                return True
            except Exception as ex:
                logger.warning("redis limiter failed (%s); falling back to in-proc", ex)

        b = self._mem.get(key)
        if b is None:
            b = _Bucket(tokens=float(limit), updated_at=now)
            self._mem[key] = b
        # refill
        b.tokens = min(float(limit), b.tokens + (now - b.updated_at) * rate)
        b.updated_at = now
        if b.tokens < 1.0:
            return False
        b.tokens -= 1.0
        return True
