from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from dubbing_pipeline.web.routes_ui import _audit_ui_page_view, _current_user_optional, _render

router = APIRouter(prefix="/system", tags=["system-ui"])


@router.get("/readiness")
async def ui_system_readiness(request: Request) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    try:
        if not (user.role and user.role.value == "admin"):
            return RedirectResponse(url="/ui/dashboard", status_code=302)
    except Exception:
        raise HTTPException(status_code=403, detail="Forbidden") from None
    with __import__("contextlib").suppress(Exception):
        _audit_ui_page_view(request, user_id=str(user.id), page="system_readiness")
    return _render(request, "system_readiness.html", {})


@router.get("/security")
async def ui_system_security_posture(request: Request) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    try:
        if not (user.role and user.role.value == "admin"):
            return RedirectResponse(url="/ui/dashboard", status_code=302)
    except Exception:
        raise HTTPException(status_code=403, detail="Forbidden") from None
    with __import__("contextlib").suppress(Exception):
        _audit_ui_page_view(request, user_id=str(user.id), page="system_security_posture")
    return _render(request, "system_security_posture.html", {})
