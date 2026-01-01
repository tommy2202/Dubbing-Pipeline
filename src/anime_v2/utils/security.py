from __future__ import annotations

import hmac
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from anime_v2.utils.config import get_settings
from anime_v2.utils.log import logger


def _client_ip(request: Request) -> str:
    # Keep it simple; avoid trusting X-Forwarded-For by default.
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _get_token(request: Request) -> str | None:
    t = request.query_params.get("token")
    if t:
        return t
    c = request.cookies.get("auth")
    if c:
        return c
    return None


@dataclass
class _TokenBucket:
    capacity: float
    refill_per_sec: float
    tokens: float
    last_ts: float

    def refill(self, now: float) -> None:
        dt = max(0.0, now - self.last_ts)
        self.tokens = min(self.capacity, self.tokens + dt * self.refill_per_sec)
        self.last_ts = now

    def take(self, n: float, now: float) -> bool:
        self.refill(now)
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


# Rate limit ONLY auth failures: 10 per minute per IP
_FAIL_BUCKETS: dict[str, _TokenBucket] = {}
_FAIL_CAPACITY = 10.0
_FAIL_REFILL_PER_SEC = 10.0 / 60.0


def _allow_failure(ip: str) -> bool:
    now = time.monotonic()
    b = _FAIL_BUCKETS.get(ip)
    if b is None:
        b = _TokenBucket(
            capacity=_FAIL_CAPACITY,
            refill_per_sec=_FAIL_REFILL_PER_SEC,
            tokens=_FAIL_CAPACITY,
            last_ts=now,
        )
        _FAIL_BUCKETS[ip] = b
    return b.take(1.0, now)


def verify_api_key(request: Request) -> None:
    """
    Auth dependency: accepts token via query (?token=...) or cookie (auth).

    - Invalid/missing token: 401
    - Brute-force failures: 429 after 10 failures/min per IP
    """
    ip = _client_ip(request)
    token = _get_token(request)
    expected = get_settings().api_token

    ok = token is not None and hmac.compare_digest(token, expected)
    if ok:
        return

    # Rate limit failures
    if not _allow_failure(ip):
        logger.warning("rate_limit auth_fail ip=%s", ip)
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many auth failures")

    # Never log the token value.
    logger.info("auth_fail ip=%s path=%s", ip, request.url.path)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

