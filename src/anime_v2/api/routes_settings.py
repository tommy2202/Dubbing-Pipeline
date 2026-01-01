from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from anime_v2.api.deps import Identity, current_identity, require_role
from anime_v2.api.models import Role
from anime_v2.api.security import verify_csrf
from anime_v2.config import get_settings
from anime_v2.utils.net import egress_guard


def _settings_path() -> Path:
    # Allow override for tests / deployments.
    p = Path(str(Path.home())) / ".anime_v2" / "settings.json"
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
            "email": "",
            "discord_webhook": "",
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


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email(v: str) -> str:
    vv = (v or "").strip()
    if not vv:
        return ""
    if len(vv) > 320 or not _EMAIL_RE.match(vv):
        raise HTTPException(status_code=400, detail="Invalid email")
    return vv


def _validate_discord_webhook(v: str) -> str:
    vv = (v or "").strip()
    if not vv:
        return ""
    # Basic safety: require https and a reasonable length; do not over-validate.
    if not (vv.startswith("https://") and len(vv) < 2048):
        raise HTTPException(status_code=400, detail="Invalid Discord webhook URL")
    return vv


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
        out_notif: dict[str, Any] = {}
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
            if "email" in n:
                out_notif["email"] = _validate_email(str(n.get("email") or ""))
            if "discord_webhook" in n:
                out_notif["discord_webhook"] = _validate_discord_webhook(
                    str(n.get("discord_webhook") or "")
                )

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
            if out_defaults:
                if not isinstance(cur.get("defaults"), dict):
                    cur["defaults"] = {}
                cur["defaults"].update(out_defaults)
            if out_notif:
                if not isinstance(cur.get("notifications"), dict):
                    cur["notifications"] = {}
                cur["notifications"].update(out_notif)
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


@router.post("/api/settings/notifications/test")
async def test_notifications(
    request: Request, ident: Identity = Depends(require_role(Role.operator))
) -> dict[str, Any]:
    store = _get_user_settings_store(request)
    cfg = store.get_user(ident.user.id)
    webhook = ""
    try:
        notif = cfg.get("notifications") if isinstance(cfg.get("notifications"), dict) else {}
        webhook = str((notif or {}).get("discord_webhook") or "").strip()
    except Exception:
        webhook = ""
    if not webhook:
        raise HTTPException(status_code=400, detail="No Discord webhook configured")

    # Best-effort POST. Will be blocked if OFFLINE_MODE or ALLOW_EGRESS=0.
    with egress_guard():
        payload = json.dumps({"content": "anime_v2: test notification"}).encode("utf-8")
        req = urllib.request.Request(
            webhook,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                code = int(getattr(resp, "status", 0) or 0)
                if code and code >= 400:
                    raise HTTPException(status_code=502, detail=f"Webhook failed ({code})")
        except HTTPException:
            raise
        except Exception as ex:
            raise HTTPException(status_code=502, detail=f"Webhook failed: {ex}") from ex

    return {"ok": True}


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
        "updated_at": updated.get("updated_at"),
    }
