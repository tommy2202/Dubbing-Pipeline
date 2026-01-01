from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from anime_v2.api.deps import current_identity
from anime_v2.api.security import issue_csrf_token
from anime_v2.config import get_settings


router = APIRouter(prefix="/ui", tags=["ui"])


def _get_templates(request: Request) -> Jinja2Templates:
    t = getattr(request.app.state, "templates", None)
    if t is None:
        raise HTTPException(status_code=500, detail="Templates not initialized")
    return t


def _current_user_optional(request: Request):
    try:
        store = getattr(request.app.state, "auth_store", None)
        if store is None:
            return None
        ident = current_identity(request, store)
        return ident.user
    except Exception:
        return None


def _with_csrf_cookie(resp, csrf_token: str) -> None:
    s = get_settings()
    resp.set_cookie(
        "csrf",
        csrf_token,
        httponly=False,
        samesite="lax",
        secure=bool(s.cookie_secure),
        max_age=int(s.refresh_token_days) * 86400,
        path="/",
    )


def _render(request: Request, template: str, ctx: dict[str, Any]) -> HTMLResponse:
    templates = _get_templates(request)
    user = _current_user_optional(request)
    csrf = issue_csrf_token()
    context = {"request": request, "user": user, "csrf_token": csrf, **(ctx or {})}
    resp = templates.TemplateResponse(request, template, context)
    _with_csrf_cookie(resp, csrf)
    return resp


@router.get("/health")
async def ui_health() -> HTMLResponse:
    return HTMLResponse("ok")


@router.get("/login")
async def ui_login(request: Request) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is not None:
        return RedirectResponse(url="/ui/dashboard", status_code=302)
    return _render(request, "login.html", {})


@router.get("/dashboard")
async def ui_dashboard(request: Request) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    return _render(request, "dashboard.html", {})


@router.get("/partials/jobs_table")
async def ui_jobs_table(request: Request, status: str | None = None, q: str | None = None, limit: int = 25) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    store = getattr(request.app.state, "job_store", None)
    if store is None:
        raise HTTPException(status_code=500, detail="Job store not initialized")
    # mirror API defaults
    limit_i = max(1, min(200, int(limit)))
    jobs = store.list(limit=1000, state=(status or None))
    if q:
        qq = str(q).lower().strip()
        if qq:
            jobs = [j for j in jobs if (qq in j.id.lower()) or (qq in (j.video_path or "").lower())]
    jobs = jobs[:limit_i]
    # Template expects simple dicts with state as string.
    return _render(request, "_jobs_table.html", {"jobs": [j.to_dict() for j in jobs]})


@router.get("/jobs/{job_id}")
async def ui_job_detail(request: Request, job_id: str) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    created = (request.query_params.get("created") or "").strip() == "1"
    return _render(request, "job_detail.html", {"job_id": job_id, "created": created})


@router.get("/upload")
async def ui_upload(request: Request) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    return _render(request, "upload_wizard.html", {})

