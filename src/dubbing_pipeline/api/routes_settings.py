from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from dubbing_pipeline.api.deps import Identity, current_identity, require_role
from dubbing_pipeline.api.models import Role
from dubbing_pipeline.api.security import verify_csrf
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.notify.settings import allowed_topics, validate_topic


def _settings_path() -> Path:
    # Allow override for tests / deployments.
    p = Path(str(Path.home())) / ".dubbing_pipeline" / "settings.json"
    s = get_settings()
    if s.user_settings_path:
        p = Path(s.user_settings_path)
    return p.expanduser().resolve()


def _now_ts() -> float:
    return float(time.time())


def _default_user_settings() -> dict[str, Any]:
    s = get_settings()
    return {
        "defaults": {
            "mode": "medium",
            "device": "auto",
            "src_lang": "ja",
            "tgt_lang": "en",
            "tts_lang": str(s.tts_lang or "en"),
            "tts_speaker": str(s.tts_speaker or "default"),
        },
        "notifications": {
            "notify_enabled": False,
            "notify_topic": "",
        },
        "library": {
            "last_series": {
                "series_slug": "",
                "series_title": "",
                "updated_at": 0.0,
            }
        },
        "updated_at": _now_ts(),
    }


def _validate_mode(v: str) -> str:
    vv = (v or "").strip().lower()
    if vv not in {"high", "medium", "low"}:
        raise HTTPException(status_code=400, detail="Invalid mode (expected high|medium|low)")
    return vv


def _validate_device(v: str) -> str:
    vv = (v or "").strip().lower()
    if vv not in {"auto", "cpu", "cuda"}:
        raise HTTPException(status_code=400, detail="Invalid device (expected auto|cpu|cuda)")
    return vv


def _validate_lang(v: str) -> str:
    vv = (v or "").strip().lower()
    if not vv or len(vv) > 12:
        raise HTTPException(status_code=400, detail="Invalid language code")
    return vv


def _validate_bool(v: Any, *, field: str) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    vv = str(v).strip().lower()
    if vv in {"1", "true", "yes", "on"}:
        return True
    if vv in {"0", "false", "no", "off"}:
        return False
    raise HTTPException(status_code=400, detail=f"Invalid {field} (expected true|false)")


def _validate_notify_topic(v: Any) -> str:
    allowed = allowed_topics()
    try:
        return validate_topic(str(v or ""), allowed=allowed)
    except ValueError as ex:
        if str(ex) == "topic_not_allowed":
            raise HTTPException(
                status_code=400,
                detail="Notification topic not in allowed list",
            ) from ex
        raise HTTPException(status_code=400, detail="Invalid notification topic") from ex


def _validate_series_slug(v: Any) -> str:
    vv = str(v or "").strip()
    if len(vv) > 200:
        raise HTTPException(status_code=400, detail="series_slug too long")
    return vv


def _validate_series_title(v: Any) -> str:
    vv = str(v or "").strip()
    if len(vv) > 200:
        raise HTTPException(status_code=400, detail="series_title too long")
    return vv


#
# NOTE (hardening sweep):
# User-level outbound notification channels (email/Discord webhooks) are intentionally
# not supported in the hardened server. Job-finish notifications are handled by the
# optional, private self-hosted `ntfy` integration (see `dubbing_pipeline.notify` + docs).
#


@dataclass
class UserSettingsStore:
    path: Path
    _lock: threading.Lock

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _settings_path()
        self._lock = threading.Lock()

    def _load_all(self) -> dict[str, Any]:
        p = self.path
        if not p.exists():
            return {"version": 1, "users": {}}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"version": 1, "users": {}}
            if not isinstance(data.get("users"), dict):
                data["users"] = {}
            data.setdefault("version", 1)
            return data
        except Exception:
            return {"version": 1, "users": {}}

    def _save_all(self, data: dict[str, Any]) -> None:
        p = self.path
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp.replace(p)

    def get_user(self, user_id: str) -> dict[str, Any]:
        uid = str(user_id or "").strip()
        if not uid:
            raise HTTPException(status_code=400, detail="Missing user id")
        with self._lock:
            all_data = self._load_all()
            users = all_data.get("users") if isinstance(all_data.get("users"), dict) else {}
            cur = users.get(uid) if isinstance(users, dict) else None
            base = _default_user_settings()
            if isinstance(cur, dict):
                # merge shallowly
                if isinstance(cur.get("defaults"), dict):
                    base["defaults"].update(cur["defaults"])
                if isinstance(cur.get("notifications"), dict):
                    base["notifications"].update(cur["notifications"])
            if isinstance(cur.get("library"), dict):
                base["library"].update(cur["library"])
                if cur.get("updated_at"):
                    base["updated_at"] = cur.get("updated_at")
            return base

    def update_user(self, user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        uid = str(user_id or "").strip()
        if not uid:
            raise HTTPException(status_code=400, detail="Missing user id")
        if not isinstance(patch, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        # Validate + normalize patch
        out_defaults: dict[str, Any] = {}
        out_notifications: dict[str, Any] = {}
        out_library: dict[str, Any] = {}
        if isinstance(patch.get("defaults"), dict):
            d = patch["defaults"]
            if "mode" in d:
                out_defaults["mode"] = _validate_mode(str(d.get("mode") or ""))
            if "device" in d:
                out_defaults["device"] = _validate_device(str(d.get("device") or ""))
            if "src_lang" in d:
                out_defaults["src_lang"] = _validate_lang(str(d.get("src_lang") or ""))
            if "tgt_lang" in d:
                out_defaults["tgt_lang"] = _validate_lang(str(d.get("tgt_lang") or ""))
            if "tts_lang" in d:
                out_defaults["tts_lang"] = _validate_lang(str(d.get("tts_lang") or ""))
            if "tts_speaker" in d:
                out_defaults["tts_speaker"] = str(d.get("tts_speaker") or "").strip() or "default"

        if isinstance(patch.get("notifications"), dict):
            n = patch["notifications"]
            if "notify_enabled" in n:
                out_notifications["notify_enabled"] = _validate_bool(
                    n.get("notify_enabled"), field="notify_enabled"
                )
            if "notify_topic" in n:
                out_notifications["notify_topic"] = _validate_notify_topic(n.get("notify_topic"))
        if isinstance(patch.get("library"), dict):
            lib = patch["library"]
            if "last_series" in lib:
                last = lib.get("last_series")
                if last is None:
                    out_library["last_series"] = {}
                elif isinstance(last, dict):
                    slug = _validate_series_slug(last.get("series_slug"))
                    title = _validate_series_title(last.get("series_title"))
                    if slug:
                        out_library["last_series"] = {
                            "series_slug": slug,
                            "series_title": title,
                            "updated_at": _now_ts(),
                        }
                    else:
                        out_library["last_series"] = {}

        with self._lock:
            all_data = self._load_all()
            users = all_data.get("users")
            if not isinstance(users, dict):
                users = {}
                all_data["users"] = users
            cur = users.get(uid)
            if not isinstance(cur, dict):
                cur = {}
            cur.setdefault("defaults", {})
            cur.setdefault("notifications", {})
            cur.setdefault("library", {})
            if out_defaults:
                if not isinstance(cur.get("defaults"), dict):
                    cur["defaults"] = {}
                cur["defaults"].update(out_defaults)
            if out_notifications:
                if not isinstance(cur.get("notifications"), dict):
                    cur["notifications"] = {}
                cur["notifications"].update(out_notifications)
            if out_library:
                if not isinstance(cur.get("library"), dict):
                    cur["library"] = {}
                cur["library"].update(out_library)
            cur["updated_at"] = _now_ts()
            users[uid] = cur
            self._save_all(all_data)

        return self.get_user(uid)


router = APIRouter(tags=["settings"])


def _get_user_settings_store(request: Request) -> UserSettingsStore:
    st = getattr(request.app.state, "user_settings_store", None)
    if st is None:
        st = UserSettingsStore()
        request.app.state.user_settings_store = st
    return st


def _enforce_csrf_if_needed(request: Request, ident: Identity) -> None:
    # Mirror CSRF policy: enforce for browser/cookie sessions; exempt API keys.
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if ident.kind == "api_key":
        return
    has_origin = bool(request.headers.get("origin"))
    uses_cookies = bool(request.cookies.get("session") or request.cookies.get("refresh"))
    if has_origin or uses_cookies:
        verify_csrf(request)


@router.get("/api/settings")
async def get_settings_me(
    request: Request, ident: Identity = Depends(current_identity)
) -> dict[str, Any]:
    store = _get_user_settings_store(request)
    user_cfg = store.get_user(ident.user.id)
    s = get_settings()
    base = str(getattr(s, "ntfy_base_url", "") or "").strip()
    return {
        "defaults": user_cfg.get("defaults", {}),
        "notifications": user_cfg.get("notifications", {}),
        "library": user_cfg.get("library", {}),
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
                "ntfy_ready": bool(getattr(s, "ntfy_enabled", False)) and bool(base),
                "ntfy_allowed_topics": allowed_topics(),
            },
        },
        "updated_at": user_cfg.get("updated_at"),
    }


@router.put("/api/settings")
async def put_settings_me(
    request: Request, ident: Identity = Depends(require_role(Role.operator))
) -> dict[str, Any]:
    body = await request.json()
    store = _get_user_settings_store(request)
    updated = store.update_user(ident.user.id, body if isinstance(body, dict) else {})
    return {
        "defaults": updated.get("defaults", {}),
        "notifications": updated.get("notifications", {}),
        "library": updated.get("library", {}),
        "updated_at": updated.get("updated_at"),
    }


@router.get("/api/admin/users/{user_id}/settings")
async def admin_get_user_settings(
    request: Request, user_id: str, _: Identity = Depends(require_role(Role.admin))
) -> dict[str, Any]:
    store = _get_user_settings_store(request)
    cfg = store.get_user(str(user_id))
    return {
        "user_id": str(user_id),
        "defaults": cfg.get("defaults", {}),
        "notifications": cfg.get("notifications", {}),
        "library": cfg.get("library", {}),
        "updated_at": cfg.get("updated_at"),
    }


@router.put("/api/admin/users/{user_id}/settings")
async def admin_put_user_settings(
    request: Request, user_id: str, _: Identity = Depends(require_role(Role.admin))
) -> dict[str, Any]:
    body = await request.json()
    store = _get_user_settings_store(request)
    updated = store.update_user(str(user_id), body if isinstance(body, dict) else {})
    return {
        "user_id": str(user_id),
        "defaults": updated.get("defaults", {}),
        "notifications": updated.get("notifications", {}),
        "library": updated.get("library", {}),
        "updated_at": updated.get("updated_at"),
    }
