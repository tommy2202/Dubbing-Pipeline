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
from dubbing_pipeline.api.routes_system import _can_import, _whisper_model_cached
from dubbing_pipeline.api.security import issue_csrf_token
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.doctor import container as doctor_container
from dubbing_pipeline.doctor import models as doctor_models
from dubbing_pipeline.doctor import wizard as doctor_wizard
from dubbing_pipeline.jobs.models import Job
from dubbing_pipeline.library import queries
from dubbing_pipeline.library.paths import get_job_output_root
from dubbing_pipeline.utils.doctor_types import CheckResult
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


def _dedupe_steps(steps: list[str]) -> list[str]:
    out: list[str] = []
    seen = set()
    for step in steps:
        s = str(step or "").strip()
        if not s or s in seen:
            continue
        out.append(s)
        seen.add(s)
    return out


def _normalize_enable_steps(steps: list[str]) -> list[str]:
    out: list[str] = []
    for step in steps:
        raw = str(step or "").strip()
        if not raw:
            continue
        if raw.lower().startswith("set ") and "=" in raw:
            rest = raw[4:].strip().rstrip(".")
            out.append(f"export {rest}")
        else:
            out.append(raw)
    return out


def _collect_steps(res: CheckResult) -> list[str]:
    steps: list[str] = []
    if isinstance(res.remediation, list):
        steps.extend([str(s) for s in res.remediation])
    details = res.details if isinstance(res.details, dict) else {}
    enable_steps = details.get("enable_steps")
    if isinstance(enable_steps, list):
        steps.extend(_normalize_enable_steps(enable_steps))
    install_steps = details.get("install_steps")
    if isinstance(install_steps, dict):
        if "linux" in install_steps and isinstance(install_steps["linux"], list):
            steps.extend([str(s) for s in install_steps["linux"]])
        else:
            for v in install_steps.values():
                if isinstance(v, list):
                    steps.extend([str(s) for s in v])
                    break
    elif isinstance(install_steps, list):
        steps.extend([str(s) for s in install_steps])
    return _dedupe_steps(steps)


def _status_class(status: str) -> str:
    s = str(status).upper()
    if s == "READY":
        return "border-emerald-600 text-emerald-200 bg-emerald-950/30"
    if s == "BROKEN":
        return "border-red-700 text-red-200 bg-red-950/30"
    return "border-amber-700 text-amber-200 bg-amber-950/30"


def _aggregate_checks(
    checks: list[CheckResult],
    *,
    match_fn,
    agg_id: str,
    name: str,
) -> CheckResult:
    selected = [c for c in checks if match_fn(c)]
    if not selected:
        return CheckResult(
            id=agg_id,
            name=name,
            status="FAIL",
            details={"error": "missing_checks"},
            remediation=[],
        )
    statuses = {c.status for c in selected}
    if "FAIL" in statuses:
        status = "FAIL"
    elif "WARN" in statuses:
        status = "WARN"
    else:
        status = "PASS"
    remediation: list[str] = []
    for c in selected:
        remediation.extend(_collect_steps(c))
    return CheckResult(
        id=agg_id,
        name=name,
        status=status,
        details={"checks": [c.id for c in selected]},
        remediation=_dedupe_steps(remediation),
    )


def _check_whisper_large_v3() -> CheckResult:
    installed = bool(_can_import("whisper"))
    cached = bool(_whisper_model_cached("large-v3"))
    status = "PASS" if (installed and cached) else "WARN"
    remediation: list[str] = []
    if not installed:
        remediation.append("python3 -m pip install openai-whisper")
    if installed and not cached:
        remediation.append("python3 -c \"import whisper; whisper.load_model('large-v3')\"")
    return CheckResult(
        id="whisper_large_v3",
        name="Whisper large-v3 cached",
        status=status,
        details={"installed": bool(installed), "cached": bool(cached)},
        remediation=remediation,
    )


def _build_setup_sections() -> list[dict[str, Any]]:
    feature_checks = [fn() for fn in doctor_wizard.build_feature_import_checks()]
    feature_by_id = {c.id: c for c in feature_checks}

    low_model_checks = [fn() for fn in doctor_models.build_model_requirement_checks(mode="low")]
    high_model_checks = [fn() for fn in doctor_models.build_model_requirement_checks(mode="high")]

    asr_low = _aggregate_checks(
        low_model_checks,
        match_fn=lambda c: c.id == "whisper_pkg" or c.id.startswith("whisper_weights_"),
        agg_id="asr_low",
        name="ASR low-mode (Whisper)",
    )
    xtts = _aggregate_checks(
        high_model_checks,
        match_fn=lambda c: c.id in {"xtts_prereqs", "xtts_weights"},
        agg_id="xtts_voice_cloning",
        name="XTTS voice cloning",
    )

    tts = feature_by_id.get("tts_coqui", CheckResult(id="tts_coqui", name="TTS package", status="FAIL"))
    tts_details = tts.details if isinstance(tts.details, dict) else {}
    tts_enabled = bool(tts_details.get("enabled", True))
    tts_installed = bool(tts_details.get("installed", False))

    diarization = feature_by_id.get("diarization_pyannote") or feature_by_id.get("diarization_speechbrain")
    demucs = feature_by_id.get("demucs_pkg")
    wav2lip = feature_by_id.get("wav2lip")

    core_items: list[dict[str, Any]] = []
    optional_items: list[dict[str, Any]] = []

    def add_item(
        *,
        target: list[dict[str, Any]],
        title: str,
        status: str,
        explanation: str,
        remediation: list[str],
        docs_anchor: str,
        notes: list[str] | None = None,
    ) -> None:
        target.append(
            {
                "title": title,
                "status": status,
                "status_class": _status_class(status),
                "explanation": explanation,
                "remediation": _dedupe_steps(remediation),
                "docs_url": f"/docs/SETUP_WIZARD.md#{docs_anchor}" if docs_anchor else "",
                "notes": [str(n) for n in (notes or []) if str(n).strip()],
            }
        )

    ffmpeg = doctor_container.check_ffmpeg()
    add_item(
        target=core_items,
        title="ffmpeg",
        status="READY" if ffmpeg.status == "PASS" else "MISSING",
        explanation="Enables audio/video extraction, muxing, and previews.",
        remediation=_collect_steps(ffmpeg),
        docs_anchor="core-ffmpeg",
    )

    add_item(
        target=core_items,
        title="ASR low-mode",
        status="READY" if asr_low.status == "PASS" else "MISSING",
        explanation="Enables transcription in low mode (ASR).",
        remediation=_collect_steps(asr_low),
        docs_anchor="core-asr-low",
    )

    tts_status = "READY" if (tts_installed and tts_enabled) else "MISSING"
    add_item(
        target=core_items,
        title="Basic TTS",
        status=tts_status,
        explanation="Enables basic text-to-speech output.",
        remediation=_collect_steps(tts),
        docs_anchor="core-tts-basic",
    )

    writable_dirs = doctor_container.check_writable_dirs()
    add_item(
        target=core_items,
        title="Storage dirs writable",
        status="READY" if writable_dirs.status == "PASS" else "BROKEN",
        explanation="Allows uploads and outputs to be written to disk.",
        remediation=_collect_steps(writable_dirs),
        docs_anchor="core-storage",
    )

    secrets = doctor_wizard.check_security_secrets()
    add_item(
        target=core_items,
        title="Auth/CSRF configured",
        status="READY" if secrets.status == "PASS" else "MISSING",
        explanation="Enables secure sessions, CSRF protection, and API auth.",
        remediation=_collect_steps(secrets),
        docs_anchor="core-auth-csrf",
    )

    remote_access = doctor_wizard.check_remote_access_posture()
    remote_notes = []
    if isinstance(remote_access.details, dict):
        warnings = remote_access.details.get("warnings") or []
        if isinstance(warnings, list):
            remote_notes = [str(w) for w in warnings if str(w).strip()]
    add_item(
        target=core_items,
        title="Remote access mode ok",
        status="READY" if remote_access.status == "PASS" else "MISSING",
        explanation="Ensures remote access posture matches your chosen mode.",
        remediation=_collect_steps(remote_access),
        docs_anchor="core-remote-access",
        notes=remote_notes,
    )

    gpu = doctor_container.check_torch_cuda(require_gpu=False)
    add_item(
        target=optional_items,
        title="GPU acceleration",
        status="READY" if gpu.status == "PASS" else "MISSING",
        explanation="Faster transcription and TTS when CUDA is available.",
        remediation=_collect_steps(gpu),
        docs_anchor="opt-gpu",
    )

    whisper_large = _check_whisper_large_v3()
    add_item(
        target=optional_items,
        title="Whisper large-v3",
        status="READY" if whisper_large.status == "PASS" else "MISSING",
        explanation="Higher-accuracy ASR model for best transcription quality.",
        remediation=_collect_steps(whisper_large),
        docs_anchor="opt-whisper-large-v3",
    )

    add_item(
        target=optional_items,
        title="XTTS voice cloning",
        status="READY" if xtts.status == "PASS" else "MISSING",
        explanation="Voice cloning for higher-fidelity TTS.",
        remediation=_collect_steps(xtts),
        docs_anchor="opt-xtts",
    )

    if diarization is not None:
        diar_details = diarization.details if isinstance(diarization.details, dict) else {}
        diar_installed = bool(diar_details.get("installed", False))
        diar_status = "READY" if diar_installed else "MISSING"
        add_item(
            target=optional_items,
            title="Diarization",
            status=diar_status,
            explanation="Speaker separation for multi-speaker content.",
            remediation=_collect_steps(diarization),
            docs_anchor="opt-diarization",
        )

    if demucs is not None:
        demucs_details = demucs.details if isinstance(demucs.details, dict) else {}
        demucs_installed = bool(demucs_details.get("installed", False))
        demucs_status = "READY" if demucs_installed else "MISSING"
        add_item(
            target=optional_items,
            title="Vocals separation",
            status=demucs_status,
            explanation="Music/voice separation for cleaner mixes.",
            remediation=_collect_steps(demucs),
            docs_anchor="opt-separation",
        )

    if wav2lip is not None:
        wav_details = wav2lip.details if isinstance(wav2lip.details, dict) else {}
        wav_available = bool(wav_details.get("available", False))
        wav_status = "READY" if wav_available else "MISSING"
        add_item(
            target=optional_items,
            title="Lipsync plugin (Wav2Lip)",
            status=wav_status,
            explanation="Optional lip-sync enhancement for video outputs.",
            remediation=_collect_steps(wav2lip),
            docs_anchor="opt-lipsync",
        )

    redis = doctor_wizard.check_queue_redis()
    redis_details = redis.details if isinstance(redis.details, dict) else {}
    redis_configured = bool(redis_details.get("configured", False))
    redis_reachable = bool(redis_details.get("reachable", False))
    if redis.status == "PASS" or (not redis_configured):
        redis_status = "READY" if redis_reachable else "MISSING"
    else:
        redis_status = "BROKEN"
    add_item(
        target=optional_items,
        title="Redis queue",
        status=redis_status,
        explanation="Shared queue backend for multi-worker deployments.",
        remediation=_collect_steps(redis),
        docs_anchor="opt-redis",
    )

    turn = doctor_container.check_turn()
    turn_details = turn.details if isinstance(turn.details, dict) else {}
    turn_configured = bool(turn_details.get("configured", False))
    turn_ok = bool(turn_details.get("url_format_ok", False)) and bool(turn_details.get("creds_set", False))
    if not turn_configured:
        turn_status = "MISSING"
    else:
        turn_status = "READY" if turn_ok else "BROKEN"
    add_item(
        target=optional_items,
        title="TURN/WebRTC relay",
        status=turn_status,
        explanation="Relay for WebRTC when direct peer connections fail.",
        remediation=_collect_steps(turn),
        docs_anchor="opt-turn",
    )

    return [
        {"title": "Core Required", "items": core_items},
        {"title": "Optional", "items": optional_items},
    ]


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


@router.get("/setup")
async def ui_setup(request: Request) -> HTMLResponse:
    user = _current_user_optional(request)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=302)
    sections = _build_setup_sections()
    with suppress(Exception):
        _audit_ui_page_view(request, user_id=str(user.id), page="setup")
    return _render(
        request,
        "setup.html",
        {
            "sections": sections,
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
            raise HTTPException(status_code=403, detail="Forbidden")
    except Exception:
        raise HTTPException(status_code=403, detail="Forbidden") from None
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
            raise HTTPException(status_code=403, detail="Forbidden")
    except Exception:
        raise HTTPException(status_code=403, detail="Forbidden") from None
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
            raise HTTPException(status_code=403, detail="Forbidden")
    except Exception:
        raise HTTPException(status_code=403, detail="Forbidden") from None
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
            raise HTTPException(status_code=403, detail="Forbidden")
    except Exception:
        raise HTTPException(status_code=403, detail="Forbidden") from None
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
            raise HTTPException(status_code=403, detail="Forbidden")
    except Exception:
        raise HTTPException(status_code=403, detail="Forbidden") from None
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
            raise HTTPException(status_code=403, detail="Forbidden")
    except Exception:
        raise HTTPException(status_code=403, detail="Forbidden") from None
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
            raise HTTPException(status_code=403, detail="Forbidden")
    except Exception:
        raise HTTPException(status_code=403, detail="Forbidden") from None
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
