from __future__ import annotations

import uuid
from typing import Any, Callable

from fastapi import Request, Response

from anime_v2.ops import audit
from anime_v2.utils.log import request_id_var, set_request_id, set_user_id


def _new_request_id() -> str:
    return uuid.uuid4().hex


async def request_context_middleware(request: Request, call_next: Callable[[Request], Any]) -> Response:
    """
    Request-scoped context:
    - Inject X-Request-ID if absent
    - Put request_id/user_id into contextvars so all logs get correlation fields
    """
    rid = request.headers.get("x-request-id") or _new_request_id()
    set_request_id(rid)
    set_user_id(None)
    try:
        resp = await call_next(request)
        resp.headers.setdefault("x-request-id", rid)
        return resp
    finally:
        set_request_id(None)
        set_user_id(None)


def audit_event(event: str, *, request: Request, user_id: str | None, meta: dict | None = None) -> None:
    rid = request_id_var.get() or request.headers.get("x-request-id") or None
    audit.emit(event, request_id=rid, user_id=user_id, meta=meta or None)

