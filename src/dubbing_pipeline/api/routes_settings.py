from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from dubbing_pipeline.api.deps import Identity, current_identity, require_role
from dubbing_pipeline.api.models import Role
from dubbing_pipeline.api.security import verify_csrf
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.security import policy


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
            "enabled": False,
            "topic": "",
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


def _validate_notify_topic(v: str) -> str:
    vv = (v or "").strip()
    if not vv:
        return ""
    if len(vv) > 64:
        raise HTTPException(status_code=400, detail="Invalid notification topic (too long)")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", vv):
        raise HTTPException(status_code=400, detail="Invalid notification topic format")
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

        out_notifications: dict[str, Any] = {}
        if isinstance(patch.get("notifications"), dict):
            n = patch["notifications"]
            if "enabled" in n:
                out_notifications["enabled"] = bool(n.get("enabled"))
            if "topic" in n:
                out_notifications["topic"] = _validate_notify_topic(str(n.get("topic") or ""))

        if out_notifications.get("enabled") and not str(out_notifications.get("topic") or "").strip():
            raise HTTPException(status_code=400, detail="Notification topic required")

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
            if out_defaults:
                if not isinstance(cur.get("defaults"), dict):
                    cur["defaults"] = {}
                cur["defaults"].update(out_defaults)
            if out_notifications:
                if not isinstance(cur.get("notifications"), dict):
                    cur["notifications"] = {}
                cur["notifications"].update(out_notifications)
            cur["updated_at"] = _now_ts()
            users[uid] = cur
            self._save_all(all_data)

        return self.get_user(uid)


router = APIRouter(
    tags=["settings"],
    dependencies=[
        Depends(policy.require_request_allowed),
        Depends(policy.require_invite_member),
    ],
)


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
    ntfy_enabled = bool(getattr(s, "ntfy_enabled", False))
    ntfy_base = str(getattr(s, "ntfy_base_url", "") or "").strip()
    ntfy_default_topic = str(getattr(s, "ntfy_topic", "") or "").strip()
    return {
        "defaults": user_cfg.get("defaults", {}),
        "notifications": user_cfg.get("notifications", {}),
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
                "ntfy_enabled": ntfy_enabled,
                "ntfy_base_configured": bool(ntfy_base),
                "ntfy_default_topic_configured": bool(ntfy_default_topic),
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
        "updated_at": updated.get("updated_at"),
    }


@router.get("/api/admin/users/{user_id}/settings")
async def admin_get_user_settings(
    request: Request, user_id: str, _: Identity = Depends(policy.require_admin)
) -> dict[str, Any]:
    store = _get_user_settings_store(request)
    cfg = store.get_user(str(user_id))
    return {
        "user_id": str(user_id),
        "defaults": cfg.get("defaults", {}),
        "updated_at": cfg.get("updated_at"),
    }


@router.put("/api/admin/users/{user_id}/settings")
async def admin_put_user_settings(
    request: Request, user_id: str, _: Identity = Depends(policy.require_admin)
) -> dict[str, Any]:
    body = await request.json()
    store = _get_user_settings_store(request)
    updated = store.update_user(str(user_id), body if isinstance(body, dict) else {})
    return {
        "user_id": str(user_id),
        "defaults": updated.get("defaults", {}),
        "updated_at": updated.get("updated_at"),
    }
