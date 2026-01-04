from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from anime_v2.api.deps import current_identity
from anime_v2.api.routes_settings import UserSettingsStore
from anime_v2.api.security import issue_csrf_token
from anime_v2.config import get_settings
from anime_v2.utils.io import read_json

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


def _job_base_dir_from_dict(job: dict[str, Any]) -> Path:
    """
    Mirror routes_jobs._job_base_dir without importing it (avoid circular imports).
    """
    out_root = Path(get_settings().output_dir).resolve()
    out_mkv = str(job.get("output_mkv") or "").strip()
    if out_mkv:
        with suppress(Exception):
            p = Path(out_mkv)
            if p.parent.exists():
                return p.parent.resolve()
    vp = str(job.get("video_path") or "").strip()
    stem = Path(vp).stem if vp else str(job.get("id") or "job")
    return (out_root / stem).resolve()


def _qa_score_for_job_dict(job: dict[str, Any]) -> float | None:
    """
    Best-effort read of Output/<job>/qa/summary.json score for list cards.
    """
    try:
        base_dir = _job_base_dir_from_dict(job)
        p = (base_dir / "qa" / "summary.json").resolve()
        if not p.exists():
            return None
        data = read_json(p, default=None)
        if not isinstance(data, dict):
            return None
        score = data.get("score")
        if score is None:
            return None
        return float(score)
    except Exception:
        return None


def _user_settings_store(request: Request) -> UserSettingsStore | None:
    st = getattr(request.app.state, "user_settings_store", None)
    return st if isinstance(st, UserSettingsStore) else None


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
async def ui_jobs_table(
    request: Request, status: str | None = None, q: str | None = None, limit: int = 25
) -> HTMLResponse:
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
    out: list[dict[str, Any]] = []
    for j in jobs:
        d = j.to_dict()
        # mobile cards want a couple "at a glance" fields
        rt = d.get("runtime") if isinstance(d.get("runtime"), dict) else {}
        proj = ""
        if isinstance(rt, dict):
            if isinstance(rt.get("project"), dict):
                proj = str((rt.get("project") or {}).get("name") or "").strip()
            if not proj:
                proj = str(rt.get("project_name") or "").strip()
        d["project_name"] = proj
        d["qa_score"] = _qa_score_for_job_dict(d)
        out.append(d)
    return _render(request, "_jobs_table.html", {"jobs": out})


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
    # Viewer/editor are view-only: no job submissions.
    try:
        if getattr(user, "role", None) and str(user.role.value) in {"viewer", "editor"}:
            return RedirectResponse(url="/ui/dashboard", status_code=302)
    except Exception:
        pass
    defaults: dict[str, Any] = {}
    try:
        st = _user_settings_store(request)
        if st is not None:
            cfg = st.get_user(user.id)
            if isinstance(cfg.get("defaults"), dict):
                defaults = dict(cfg.get("defaults") or {})
    except Exception:
        defaults = {}
    return _render(request, "upload_wizard.html", {"user_defaults": defaults})


@router.get("/presets")
async def ui_presets(request: Request) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    return _render(request, "presets.html", {})


@router.get("/projects")
async def ui_projects(request: Request) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    return _render(request, "projects.html", {})


@router.get("/settings")
async def ui_settings(request: Request) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    st = _user_settings_store(request)
    cfg = {}
    if st is not None:
        try:
            cfg = st.get_user(user.id)
        except Exception:
            cfg = {}
    s = get_settings()
    return _render(
        request,
        "settings.html",
        {
            "cfg": cfg,
            "can_edit_settings": (
                (str(user.role.value) in {"operator", "admin"})
                if getattr(user, "role", None)
                else False
            ),
            "system": {
                "limits": {
                    "max_concurrency_global": int(s.max_concurrency_global),
                    "max_concurrency_transcribe": int(s.max_concurrency_transcribe),
                    "max_concurrency_tts": int(s.max_concurrency_tts),
                    "backpressure_q_max": int(s.backpressure_q_max),
                },
                "budgets": {
                    "budget_transcribe_sec": int(s.budget_transcribe_sec),
                    "budget_tts_sec": int(s.budget_tts_sec),
                    "budget_mux_sec": int(s.budget_mux_sec),
                },
            },
        },
    )


@router.get("/qr")
async def ui_qr_redeem(request: Request, code: str = "") -> HTMLResponse:
    # QR redeem does not require prior auth; it will call /api/auth/qr/redeem.
    c = (code or request.query_params.get("code") or "").strip()
    if not c:
        return _render(request, "qr_redeem.html", {"code": ""})
    return _render(request, "qr_redeem.html", {"code": c})
