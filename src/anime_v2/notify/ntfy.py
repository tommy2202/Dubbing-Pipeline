from __future__ import annotations

import base64
import random
import time
import urllib.error
import urllib.request
from contextlib import suppress

from anime_v2.config import get_settings
from anime_v2.ops import audit
from anime_v2.utils.log import logger

from .base import Notification


def _parse_auth(raw: str) -> dict[str, str]:
    """
    Supported formats:
      - "Bearer <token>"
      - "token:<token>"
      - "userpass:<user>:<pass>"
      - "<user>:<pass>"
    Returns headers to apply. Never returns secrets for logging.
    """
    v = (raw or "").strip()
    if not v:
        return {}
    if v.lower().startswith("bearer "):
        return {"Authorization": v}
    if v.lower().startswith("token:"):
        tok = v.split(":", 1)[1].strip()
        return {"Authorization": f"Bearer {tok}"}
    if v.lower().startswith("userpass:"):
        rest = v.split(":", 1)[1]
        parts = rest.split(":", 1)
        if len(parts) != 2:
            return {}
        user, pw = parts[0], parts[1]
        b64 = base64.b64encode(f"{user}:{pw}".encode()).decode("ascii")
        return {"Authorization": f"Basic {b64}"}
    if ":" in v and not v.startswith("http"):
        user, pw = v.split(":", 1)
        b64 = base64.b64encode(f"{user}:{pw}".encode()).decode("ascii")
        return {"Authorization": f"Basic {b64}"}
    # Unknown format; treat as bearer token for convenience.
    return {"Authorization": f"Bearer {v}"}


def _sleep_backoff(attempt: int) -> None:
    # Exponential backoff with jitter; cap to keep job worker responsive.
    base = 0.5 * (2**max(0, attempt))
    delay = min(6.0, base) + random.random() * 0.25
    time.sleep(delay)


def _post_ntfy(
    *,
    base_url: str,
    topic: str,
    payload: Notification,
    auth_headers: dict[str, str],
    timeout_sec: float,
    tls_insecure: bool,
) -> int:
    url = f"{base_url.rstrip('/')}/{topic.strip()}"
    body = (payload.message or "").encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")

    # ntfy headers: https://docs.ntfy.sh/publish/
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    req.add_header("Title", payload.title or "Notification")
    if payload.tags:
        req.add_header("Tags", ",".join([str(t).strip() for t in payload.tags if str(t).strip()]))
    if payload.priority is not None:
        # ntfy expects 1..5 (also supports "min|low|default|high|max")
        p = max(1, min(5, int(payload.priority)))
        req.add_header("Priority", str(p))
    if payload.url:
        req.add_header("Click", str(payload.url))

    for k, v in (auth_headers or {}).items():
        if k and v:
            req.add_header(k, v)

    ctx = None
    if tls_insecure:
        try:
            import ssl

            ctx = ssl._create_unverified_context()
        except Exception:
            ctx = None
    with urllib.request.urlopen(req, timeout=float(timeout_sec), context=ctx) as resp:  # type: ignore[arg-type]
        return int(getattr(resp, "status", 200) or 200)


def notify(
    *,
    event: str,
    title: str,
    message: str,
    url: str | None = None,
    tags: list[str] | None = None,
    priority: int | None = None,
    user_id: str | None = None,
    job_id: str | None = None,
) -> bool:
    """
    Best-effort ntfy notification.

    Returns:
      True if delivered (2xx), False otherwise.
    Never raises for delivery failures.
    """
    s = get_settings()
    if not bool(getattr(s, "ntfy_enabled", False)):
        return False
    base = str(getattr(s, "ntfy_base_url", "") or "").strip()
    topic = str(getattr(s, "ntfy_topic", "") or "").strip()
    if not base or not topic:
        return False

    auth_raw = None
    try:
        auth = getattr(s, "ntfy_auth", None)
        auth_raw = auth.get_secret_value() if auth is not None else None
    except Exception:
        auth_raw = None
    auth_headers = _parse_auth(auth_raw or "") if auth_raw else {}

    n = Notification(event=event, title=title, message=message, url=url, tags=tags, priority=priority)
    retries = max(0, int(getattr(s, "ntfy_retries", 3) or 0))
    timeout_sec = float(getattr(s, "ntfy_timeout_sec", 5.0) or 5.0)
    tls_insecure = bool(getattr(s, "ntfy_tls_insecure", False))

    ok = False
    last_status: int | None = None
    last_error: str | None = None
    for attempt in range(retries + 1):
        try:
            st = _post_ntfy(
                base_url=base,
                topic=topic,
                payload=n,
                auth_headers=auth_headers,
                timeout_sec=timeout_sec,
                tls_insecure=tls_insecure,
            )
            last_status = int(st)
            if 200 <= int(st) < 300:
                ok = True
                break
            # Retry on rate limit / server errors.
            if (int(st) in {408, 425, 429} or int(st) >= 500) and (attempt < retries):
                _sleep_backoff(attempt)
                continue
            break
        except urllib.error.HTTPError as ex:
            try:
                last_status = int(ex.code)
            except Exception:
                last_status = None
            last_error = "http_error"
            if (
                (last_status in {408, 425, 429} or (last_status is not None and last_status >= 500))
                and (attempt < retries)
            ):
                _sleep_backoff(attempt)
                continue
            break
        except Exception as ex:
            last_error = str(ex)[:200]
            if attempt < retries:
                _sleep_backoff(attempt)
                continue
            break

    # Audit/log without credentials (never include auth/topic).
    with suppress(Exception):
        audit.emit(
            "notify.ntfy",
            request_id=None,
            user_id=str(user_id or "") or None,
            meta={
                "ok": bool(ok),
                "event": str(event),
                "job_id": str(job_id or "") or None,
                "status": int(last_status) if last_status is not None else None,
                "error": last_error,
            },
        )

    # Structured app log (no secrets)
    with suppress(Exception):
        logger.info(
            "ntfy_notify",
            ok=bool(ok),
            event=str(event),
            job_id=str(job_id or "") or None,
            status=int(last_status) if last_status is not None else None,
        )

    return bool(ok)

