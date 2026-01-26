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
    event: str,
    *,
    request: Request,
    user_id: str | None = None,
    actor_id: str | None = None,
    target_id: str | None = None,
    outcome: str | None = None,
    meta: dict | None = None,
    meta_safe: dict | None = None,
) -> None:
    rid = (
        request_id_var.get()
        or getattr(getattr(request, "state", None), "request_id", None)
        or request.headers.get("x-request-id")
        or None
    )
    resource_id = target_id
    safe_meta = meta_safe if meta_safe is not None else meta
    if resource_id is None and isinstance(safe_meta, dict):
        for cand in ("resource_id", "job_id"):
            if safe_meta.get(cand):
                resource_id = str(safe_meta.get(cand))
                break
    audit.audit_event(
        event,
        request_id=rid,
        actor_id=actor_id or user_id,
        target_id=resource_id,
        outcome=outcome,
        metadata_safe=safe_meta,
    )
