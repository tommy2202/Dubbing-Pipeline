from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from fastapi import Request, Response

from dubbing_pipeline.ops import audit
from dubbing_pipeline.utils.log import request_id_var, set_request_id, set_user_id


def _new_request_id() -> str:
    return uuid.uuid4().hex


async def request_context_middleware(
    request: Request, call_next: Callable[[Request], Any]
) -> Response:
    """
    Request-scoped context:
    - Inject X-Request-ID if absent
    - Put request_id/user_id into contextvars so all logs get correlation fields
    """
    rid = request.headers.get("x-request-id") or _new_request_id()
    set_request_id(rid)
    set_user_id(None)
    try:
        request.state.request_id = rid
    except Exception:
        pass
    try:
        resp = await call_next(request)
        resp.headers.setdefault("x-request-id", rid)
        return resp
    finally:
        set_request_id(None)
        set_user_id(None)


def audit_event(
    event: str, *, request: Request, user_id: str | None, meta: dict | None = None
) -> None:
    rid = (
        request_id_var.get()
        or getattr(getattr(request, "state", None), "request_id", None)
        or request.headers.get("x-request-id")
        or None
    )
    resource_id = None
    if isinstance(meta, dict):
        for cand in ("resource_id", "job_id"):
            if meta.get(cand):
                resource_id = str(meta.get(cand))
                break
    audit.event(
        event,
        request_id=rid,
        actor_id=user_id,
        resource_id=resource_id,
        meta_safe=meta or None,
    )
