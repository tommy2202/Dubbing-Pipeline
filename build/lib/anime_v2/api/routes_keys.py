from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from anime_v2.api.deps import Identity, require_role
from anime_v2.api.middleware import audit_event
from anime_v2.api.models import ApiKey, AuthStore, Role, now_ts
from anime_v2.utils.crypto import hash_secret, random_id, random_prefix

router = APIRouter(prefix="/keys", tags=["api_keys"])

_ALLOWED_SCOPES = {"read:job", "submit:job", "edit:job", "admin:*"}


def _get_store(request: Request) -> AuthStore:
    s = getattr(request.app.state, "auth_store", None)
    if s is None:
        raise HTTPException(status_code=500, detail="Auth store not initialized")
    return s


@router.get("")
async def list_keys(
    request: Request, ident: Identity = Depends(require_role(Role.admin))
) -> list[dict[str, Any]]:
    store = _get_store(request)
    keys = store.list_api_keys(user_id=None)
    out = []
    for k in keys:
        out.append(
            {
                "id": k.id,
                "prefix": f"dp_{k.prefix}_...",
                "user_id": k.user_id,
                "scopes": k.scopes,
                "created_at": k.created_at,
                "revoked": k.revoked,
            }
        )
    return out


@router.post("")
async def create_key(
    request: Request, ident: Identity = Depends(require_role(Role.admin))
) -> dict[str, Any]:
    store = _get_store(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    user_id = str(body.get("user_id") or ident.user.id)
    scopes = body.get("scopes") or ["read:job"]
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise HTTPException(status_code=400, detail="Invalid scopes")
    scopes = [str(s).strip() for s in scopes if str(s).strip()]
    if not scopes:
        scopes = ["read:job"]
    bad = [s for s in scopes if s not in _ALLOWED_SCOPES]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unsupported scope(s): {', '.join(bad)}")
    prefix = random_prefix(10)
    key_plain = f"dp_{prefix}_{random_id('', 24)}"
    key_hash = hash_secret(key_plain)
    k = ApiKey(
        id=random_id("k_", 16),
        prefix=prefix,
        key_hash=key_hash,
        scopes_json=json.dumps(scopes),
        user_id=user_id,
        created_at=now_ts(),
        revoked=False,
    )
    store.create_api_key(k)
    audit_event(
        "api_key.create",
        request=request,
        user_id=ident.user.id,
        meta={"key_id": k.id, "user_id": user_id, "scopes": scopes, "prefix": f"dp_{prefix}_..."},
    )
    return {
        "id": k.id,
        "prefix": f"dp_{prefix}_...",
        "key": key_plain,
        "scopes": scopes,
        "user_id": user_id,
    }


@router.post("/{key_id}/revoke")
async def revoke_key(
    request: Request, key_id: str, ident: Identity = Depends(require_role(Role.admin))
) -> dict[str, Any]:
    store = _get_store(request)
    store.revoke_api_key(key_id)
    audit_event("api_key.revoke", request=request, user_id=ident.user.id, meta={"key_id": key_id})
    return {"ok": True}
