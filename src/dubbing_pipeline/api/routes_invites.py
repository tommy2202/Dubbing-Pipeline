from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from dubbing_pipeline.api.deps import get_limiter
from dubbing_pipeline.api.invites import invite_token_hash
from dubbing_pipeline.api.middleware import audit_event
from dubbing_pipeline.api.models import Role
from dubbing_pipeline.utils.crypto import PasswordHasher
from dubbing_pipeline.utils.net import get_client_ip

router = APIRouter(prefix="/api/invites", tags=["invites"])

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")


def _token_hash(token: str) -> str:
    return invite_token_hash(token)


def _validate_username(raw: str) -> str:
    u = str(raw or "").strip()
    if not u:
        raise HTTPException(status_code=400, detail="username required")
    if not _USERNAME_RE.fullmatch(u):
        raise HTTPException(
            status_code=400,
            detail="username must be 3-64 chars (letters, numbers, dot, underscore, dash)",
        )
    return u


def _validate_password(raw: str) -> str:
    pw = str(raw or "")
    if len(pw) < 8:
        raise HTTPException(status_code=400, detail="password too short (min 8 chars)")
    if len(pw) > 200:
        raise HTTPException(status_code=400, detail="password too long")
    return pw


@router.post("/redeem")
async def redeem_invite(request: Request) -> dict[str, Any]:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    token = str(body.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing invite token")

    username = _validate_username(body.get("username") or "")
    password = _validate_password(body.get("password") or "")

    # Rate limit redemption by IP + token hash prefix.
    rl = get_limiter(request)
    ip = get_client_ip(request)
    if not rl.allow(f"invites:redeem:ip:{ip}", limit=10, per_seconds=60):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    token_hash = _token_hash(token)
    if not rl.allow(f"invites:redeem:token:{token_hash[:12]}", limit=5, per_seconds=60):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    store = getattr(request.app.state, "auth_store", None)
    if store is None:
        raise HTTPException(status_code=500, detail="Auth store not initialized")

    user, status, invite = store.redeem_invite(
        token_hash=token_hash,
        username=username,
        password_hash=PasswordHasher().hash(password),
        role=Role.operator,
    )
    if user is None:
        if status == "username_taken":
            raise HTTPException(status_code=409, detail="Username already taken")
        if status == "used":
            raise HTTPException(status_code=410, detail="Invite already used")
        if status == "expired":
            raise HTTPException(status_code=410, detail="Invite expired")
        raise HTTPException(status_code=400, detail="Invalid invite")

    audit_event(
        "invite.redeem",
        request=request,
        user_id=user.id,
        meta={
            "created_by": str(invite.get("created_by") or "") if isinstance(invite, dict) else "",
            "expires_at": int(invite.get("expires_at") or 0) if isinstance(invite, dict) else 0,
        },
    )
    return {"ok": True, "user_id": user.id, "username": user.username}

