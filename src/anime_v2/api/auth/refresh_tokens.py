from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

from anime_v2.api.models import AuthStore, now_ts
from anime_v2.api.security import create_refresh_token, decode_token


class RefreshTokenError(RuntimeError):
    pass


def _hash_token(token: str) -> str:
    # Fast, deterministic. Refresh tokens are still JWT-signed; this is a DB lookup key.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_and_store_refresh_token(*, store: AuthStore, user_id: str, days: int) -> str:
    tok = create_refresh_token(sub=user_id, days=int(days))
    data = decode_token(tok, expected_typ="refresh")
    jti = str(data.get("jti") or "")
    if not jti:
        raise RefreshTokenError("refresh token missing jti")
    exp = int(data.get("exp") or 0)
    if exp <= 0:
        # fall back to days if missing
        exp = int(time.time()) + int(days) * 86400
    store.put_refresh_token(
        jti=jti,
        user_id=user_id,
        token_hash=_hash_token(tok),
        expires_at=int(exp),
        created_at=now_ts(),
    )
    return tok


@dataclass(frozen=True, slots=True)
class RotateResult:
    access_sub: str
    old_jti: str
    new_refresh_token: str


def rotate_refresh_token(*, store: AuthStore, refresh_token: str, days: int) -> RotateResult:
    data = decode_token(refresh_token, expected_typ="refresh")
    sub = str(data.get("sub") or "")
    jti = str(data.get("jti") or "")
    if not sub or not jti:
        raise RefreshTokenError("invalid refresh token claims")

    rec = store.get_refresh_token(jti)
    if rec is None:
        raise RefreshTokenError("unknown refresh token")

    # Enforce single-use rotation.
    if bool(rec.get("revoked")):
        # If we have a replaced_by, treat this as potential replay.
        if rec.get("replaced_by"):
            store.revoke_all_refresh_tokens_for_user(sub)
            raise RefreshTokenError("refresh token replay detected; all sessions revoked")
        raise RefreshTokenError("refresh token revoked")

    # Verify token hash matches the stored record.
    if str(rec.get("token_hash") or "") != _hash_token(refresh_token):
        store.revoke_all_refresh_tokens_for_user(sub)
        raise RefreshTokenError("refresh token mismatch; all sessions revoked")

    # Expiry guard
    exp = int(rec.get("expires_at") or 0)
    if exp and int(time.time()) > exp:
        store.revoke_refresh_token(jti)
        raise RefreshTokenError("refresh token expired")

    # Rotate: mint new token and mark old as replaced.
    new_tok = issue_and_store_refresh_token(store=store, user_id=sub, days=int(days))
    new_data: dict[str, Any] = decode_token(new_tok, expected_typ="refresh")
    new_jti = str(new_data.get("jti") or "")
    store.rotate_refresh_token(old_jti=jti, new_jti=new_jti)
    return RotateResult(access_sub=sub, old_jti=jti, new_refresh_token=new_tok)


def revoke_refresh_token_best_effort(*, store: AuthStore, refresh_token: str) -> None:
    try:
        data = decode_token(refresh_token, expected_typ="refresh")
        jti = str(data.get("jti") or "")
        if jti:
            store.revoke_refresh_token(jti)
    except Exception:
        return

