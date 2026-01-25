from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from dubbing_pipeline.api.access import require_job_access, require_library_access
from dubbing_pipeline.api.deps import current_identity
from dubbing_pipeline.api.routes_settings import UserSettingsStore
from dubbing_pipeline.api.security import issue_csrf_token
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job
from dubbing_pipeline.library import queries
from dubbing_pipeline.library.paths import get_job_output_root
from dubbing_pipeline.utils.io import read_json
from dubbing_pipeline.ops import audit

router = APIRouter(prefix="/ui", tags=["ui"])
public_router = APIRouter(tags=["ui"])


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
    # Queue banner (Redis down -> fallback queue). Best-effort; must never break UI rendering.
    qb = getattr(request.app.state, "queue_backend", None)
    banner = None
    mode = "unknown"
    with suppress(Exception):
        if qb is not None:
            st = qb.status()
            banner = getattr(st, "banner", None)
            mode = str(getattr(st, "mode", "unknown") or "unknown")
    context = {
        "request": request,
        "user": user,
        "csrf_token": csrf,
        "queue_banner": banner,
        "queue_mode": mode,
        **(ctx or {}),
    }
    resp = templates.TemplateResponse(request, template, context)
    _with_csrf_cookie(resp, csrf)
    return resp


def _audit_ui_page_view(request: Request, *, user_id: str, page: str, meta: dict[str, Any] | None = None) -> None:
    """
    UI page view audit logging is opt-in to avoid noise.
    """
    s = get_settings()
    if not bool(getattr(s, "ui_audit_page_views", False)):
        return
    try:
        audit.emit(
            "ui.page_view",
            request_id=None,
            user_id=str(user_id),
            meta={"page": str(page), "path": str(request.url.path), **(meta or {})},
        )
    except Exception:
        return


def _job_base_dir_from_dict(job: dict[str, Any]) -> Path:
    """
    Canonical job output root resolution for UI helpers.
    """
    try:
        j = Job.from_dict(job)
        return get_job_output_root(j)
    except Exception:
        # Last-resort fallback for malformed dicts.
        out_root = Path(get_settings().output_dir).resolve()
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
    with suppress(Exception):
        _audit_ui_page_view(request, user_id=str(user.id), page="dashboard")
    return _render(request, "dashboard.html", {})


@router.get("/library")
async def ui_library_series(request: Request) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    with suppress(Exception):
        _audit_ui_page_view(request, user_id=str(user.id), page="library_series")
    return _render(request, "library_series.html", {})


@router.get("/library/{series_slug}")
async def ui_library_seasons(request: Request, series_slug: str) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    try:
        store = getattr(request.app.state, "job_store", None)
        auth_store = getattr(request.app.state, "auth_store", None)
        if store is None or auth_store is None:
            raise HTTPException(status_code=500, detail="Store not initialized")
        ident = current_identity(request, auth_store)
        require_library_access(
            store=store, ident=ident, series_slug=series_slug, allow_shared_read=True
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=403, detail="Forbidden") from None
    with suppress(Exception):
        _audit_ui_page_view(
            request, user_id=str(user.id), page="library_seasons", meta={"series_slug": series_slug}
        )
    return _render(request, "library_seasons.html", {"series_slug": series_slug})


@router.get("/library/{series_slug}/season/{season_number}")
async def ui_library_episodes(request: Request, series_slug: str, season_number: int) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    try:
        store = getattr(request.app.state, "job_store", None)
        auth_store = getattr(request.app.state, "auth_store", None)
        if store is None or auth_store is None:
            raise HTTPException(status_code=500, detail="Store not initialized")
        ident = current_identity(request, auth_store)
        require_library_access(
            store=store,
            ident=ident,
            series_slug=series_slug,
            season_number=int(season_number),
            allow_shared_read=True,
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=403, detail="Forbidden") from None
    with suppress(Exception):
        _audit_ui_page_view(
            request,
            user_id=str(user.id),
            page="library_episodes",
            meta={"series_slug": series_slug, "season_number": int(season_number)},
        )
    return _render(
        request,
        "library_episodes.html",
        {"series_slug": series_slug, "season_number": int(season_number)},
    )


@router.get("/library/{series_slug}/season/{season_number}/episode/{episode_number}")
async def ui_library_episode_detail(
    request: Request, series_slug: str, season_number: int, episode_number: int
) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    try:
        store = getattr(request.app.state, "job_store", None)
        auth_store = getattr(request.app.state, "auth_store", None)
        if store is None or auth_store is None:
            raise HTTPException(status_code=500, detail="Store not initialized")
        ident = current_identity(request, auth_store)
        require_library_access(
            store=store,
            ident=ident,
            series_slug=series_slug,
            season_number=int(season_number),
            episode_number=int(episode_number),
            allow_shared_read=True,
        )
        with suppress(Exception):
            job_id = queries.latest_episode_job_id(
                store=store,
                ident=ident,
                series_slug=series_slug,
                season_number=int(season_number),
                episode_number=int(episode_number),
            )
            store.record_view(
                user_id=str(ident.user.id),
                series_slug=series_slug,
                season_number=int(season_number),
                episode_number=int(episode_number),
                job_id=job_id,
            )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=403, detail="Forbidden") from None
    with suppress(Exception):
        _audit_ui_page_view(
            request,
            user_id=str(user.id),
            page="library_episode_detail",
            meta={
                "series_slug": series_slug,
                "season_number": int(season_number),
                "episode_number": int(episode_number),
            },
        )
    return _render(
        request,
        "library_episode_detail.html",
        {"series_slug": series_slug, "season_number": int(season_number), "episode_number": int(episode_number)},
    )


@router.get("/voices/{series_slug}/{voice_id}")
async def ui_voice_detail(request: Request, series_slug: str, voice_id: str) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    try:
        store = getattr(request.app.state, "job_store", None)
        auth_store = getattr(request.app.state, "auth_store", None)
        if store is None or auth_store is None:
            raise HTTPException(status_code=500, detail="Store not initialized")
        ident = current_identity(request, auth_store)
        require_library_access(store=store, ident=ident, series_slug=series_slug)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=403, detail="Forbidden") from None
    with suppress(Exception):
        _audit_ui_page_view(
            request,
            user_id=str(user.id),
            page="voice_detail",
            meta={"series_slug": series_slug, "voice_id": voice_id},
        )
    return _render(
        request,
        "voice_detail.html",
        {"series_slug": series_slug, "voice_id": voice_id},
    )


@router.get("/partials/jobs_table")
async def ui_jobs_table(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    project: str | None = None,
    mode: str | None = None,
    tag: str | None = None,
    include_archived: int = 0,
    limit: int = 25,
) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    store = getattr(request.app.state, "job_store", None)
    if store is None:
        raise HTTPException(status_code=500, detail="Job store not initialized")
    try:
        auth_store = getattr(request.app.state, "auth_store", None)
        if auth_store is None:
            raise HTTPException(status_code=500, detail="Auth store not initialized")
        ident = current_identity(request, auth_store)
    except Exception:
        return RedirectResponse(url="/ui/login", status_code=302)
    # mirror API defaults
    limit_i = max(1, min(200, int(limit)))
    jobs = store.list(limit=1000, state=(status or None))
    if not bool(int(include_archived or 0)):
        jobs = [
            j
            for j in jobs
            if not (isinstance(j.runtime, dict) and bool((j.runtime or {}).get("archived")))
        ]
    qq = str(q or "").lower().strip()
    proj_q = str(project or "").strip().lower()
    mode_q = str(mode or "").strip().lower()
    tag_q = str(tag or "").strip().lower()
    if qq or proj_q or mode_q or tag_q:
        out_jobs = []
        for j in jobs:
            rt = j.runtime if isinstance(j.runtime, dict) else {}
            proj = ""
            if isinstance(rt, dict):
                if isinstance(rt.get("project"), dict):
                    proj = str((rt.get("project") or {}).get("name") or "").strip()
                if not proj:
                    proj = str(rt.get("project_name") or "").strip()
            tags = []
            if isinstance(rt, dict) and isinstance(rt.get("tags"), list):
                tags = [str(x).strip().lower() for x in (rt.get("tags") or []) if str(x).strip()]
            if proj_q and proj_q not in proj.lower():
                continue
            if mode_q and mode_q != str(j.mode or "").strip().lower():
                continue
            if tag_q and tag_q not in set(tags):
                continue
            if qq:
                hay = " ".join([j.id, str(j.video_path or ""), proj, " ".join(tags)]).lower()
                if qq not in hay:
                    continue
            out_jobs.append(j)
        jobs = out_jobs
    visible: list[Job] = []
    for j in jobs:
        try:
            require_job_access(store=store, ident=ident, job=j)
        except HTTPException as ex:
            if ex.status_code == 403:
                continue
            raise
        visible.append(j)
    jobs = visible[:limit_i]
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
        d["tags"] = (rt.get("tags") if isinstance(rt, dict) else []) or []
        d["archived"] = bool((rt.get("archived") if isinstance(rt, dict) else False) or False)
        d["qa_score"] = _qa_score_for_job_dict(d)
        out.append(d)
    return _render(request, "_jobs_table.html", {"jobs": out})


@router.get("/models")
async def ui_models(request: Request) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    return _render(request, "models.html", {})


@router.get("/jobs/{job_id}")
async def ui_job_detail(request: Request, job_id: str) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    try:
        store = getattr(request.app.state, "job_store", None)
        auth_store = getattr(request.app.state, "auth_store", None)
        if store is None or auth_store is None:
            raise HTTPException(status_code=500, detail="Store not initialized")
        ident = current_identity(request, auth_store)
        require_job_access(store=store, ident=ident, job_id=job_id)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=403, detail="Forbidden") from None
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
                "notifications": {
                    "ntfy_enabled": bool(getattr(s, "ntfy_enabled", False)),
                    "ntfy_base_configured": bool(str(getattr(s, "ntfy_base_url", "") or "").strip()),
                    "ntfy_default_topic_configured": bool(
                        str(getattr(s, "ntfy_topic", "") or "").strip()
                    ),
                },
            },
        },
    )


@router.get("/settings/notifications")
async def ui_settings_notifications(request: Request) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    with suppress(Exception):
        _audit_ui_page_view(request, user_id=str(user.id), page="settings_notifications")
    return _render(request, "settings_notifications.html", {})


@router.get("/admin/queue")
async def ui_admin_queue(request: Request) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    try:
        if not (user.role and user.role.value == "admin"):
            return RedirectResponse(url="/ui/dashboard", status_code=302)
    except Exception:
        return RedirectResponse(url="/ui/dashboard", status_code=302)
    with suppress(Exception):
        _audit_ui_page_view(request, user_id=str(user.id), page="admin_queue")
    return _render(request, "admin_queue.html", {})


@router.get("/admin/dashboard")
async def ui_admin_dashboard(request: Request) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    try:
        if not (user.role and user.role.value == "admin"):
            return RedirectResponse(url="/ui/dashboard", status_code=302)
    except Exception:
        return RedirectResponse(url="/ui/dashboard", status_code=302)
    with suppress(Exception):
        _audit_ui_page_view(request, user_id=str(user.id), page="admin_dashboard")
    return _render(request, "admin_dashboard.html", {})


@router.get("/admin/reports")
async def ui_admin_reports(request: Request) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    try:
        if not (user.role and user.role.value == "admin"):
            return RedirectResponse(url="/ui/dashboard", status_code=302)
    except Exception:
        return RedirectResponse(url="/ui/dashboard", status_code=302)
    with suppress(Exception):
        _audit_ui_page_view(request, user_id=str(user.id), page="admin_reports")
    return _render(request, "admin_reports.html", {})


@router.get("/admin/glossaries")
async def ui_admin_glossaries(request: Request) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    try:
        if not (user.role and user.role.value == "admin"):
            return RedirectResponse(url="/ui/dashboard", status_code=302)
    except Exception:
        return RedirectResponse(url="/ui/dashboard", status_code=302)
    with suppress(Exception):
        _audit_ui_page_view(request, user_id=str(user.id), page="admin_glossaries")
    return _render(request, "admin_glossaries.html", {})


@router.get("/admin/pronunciation")
async def ui_admin_pronunciation(request: Request) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    try:
        if not (user.role and user.role.value == "admin"):
            return RedirectResponse(url="/ui/dashboard", status_code=302)
    except Exception:
        return RedirectResponse(url="/ui/dashboard", status_code=302)
    with suppress(Exception):
        _audit_ui_page_view(request, user_id=str(user.id), page="admin_pronunciation")
    return _render(request, "admin_pronunciation.html", {})


@router.get("/admin/voice-suggestions")
async def ui_admin_voice_suggestions(request: Request) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    try:
        if not (user.role and user.role.value == "admin"):
            return RedirectResponse(url="/ui/dashboard", status_code=302)
    except Exception:
        return RedirectResponse(url="/ui/dashboard", status_code=302)
    with suppress(Exception):
        _audit_ui_page_view(request, user_id=str(user.id), page="admin_voice_suggestions")
    return _render(request, "admin_voice_suggestions.html", {})


@router.get("/admin/invites")
async def ui_admin_invites(request: Request) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    try:
        if not (user.role and user.role.value == "admin"):
            return RedirectResponse(url="/ui/dashboard", status_code=302)
    except Exception:
        return RedirectResponse(url="/ui/dashboard", status_code=302)
    with suppress(Exception):
        _audit_ui_page_view(request, user_id=str(user.id), page="admin_invites")
    return _render(request, "admin_invites.html", {})


@router.get("/qr")
async def ui_qr_redeem(request: Request, code: str = "") -> HTMLResponse:
    # QR redeem does not require prior auth; it will call /api/auth/qr/redeem.
    c = (code or request.query_params.get("code") or "").strip()
    if not c:
        return _render(request, "qr_redeem.html", {"code": ""})
    return _render(request, "qr_redeem.html", {"code": c})


@public_router.get("/invite/{token}")
async def ui_invite_redeem(request: Request, token: str) -> HTMLResponse:
    # Invite redeem does not require prior auth; it will call /api/invites/redeem.
    tok = str(token or "").strip()
    return _render(request, "invite_redeem.html", {"token": tok})
