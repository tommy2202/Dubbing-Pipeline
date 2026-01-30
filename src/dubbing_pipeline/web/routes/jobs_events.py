from __future__ import annotations

import asyncio
import ipaddress
from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from sse_starlette.sse import EventSourceResponse  # type: ignore

from dubbing_pipeline.api.access import require_job_access
from dubbing_pipeline.api.deps import Identity, require_scope
from dubbing_pipeline.api.models import AuthStore
from dubbing_pipeline.api.security import decode_token
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import JobState
from dubbing_pipeline.runtime import lifecycle
from dubbing_pipeline.security import policy
from dubbing_pipeline.security.policy_deps import secure_router
from dubbing_pipeline.utils.crypto import verify_secret
from dubbing_pipeline.utils.net import get_client_ip_from_headers
from dubbing_pipeline.web.routes.jobs_common import _get_store

router = secure_router()
ws_router = APIRouter()


@router.get("/api/jobs/events")
async def jobs_events(
    request: Request,
    ident: Identity = Depends(require_scope("read:job")),
):
    store = _get_store(request)

    async def gen():
        last: dict[str, str] = {}
        try:
            while True:
                if lifecycle.is_draining():
                    return
                if await request.is_disconnected():
                    return
                jobs = store.list(limit=200)
                for j in jobs:
                    try:
                        require_job_access(store=store, ident=ident, job=j)
                    except HTTPException as ex:
                        if ex.status_code == 403:
                            continue
                        raise
                    key = f"{j.state.value}:{j.updated_at}:{j.progress:.4f}:{j.message}"
                    if last.get(j.id) == key:
                        continue
                    last[j.id] = key
                    payload = {
                        "id": j.id,
                        "state": j.state.value,
                        "progress": float(j.progress),
                        "message": j.message,
                        "updated_at": j.updated_at,
                        "created_at": j.created_at,
                        "video_path": j.video_path,
                        "mode": j.mode,
                        "src_lang": j.src_lang,
                        "tgt_lang": j.tgt_lang,
                    }
                    yield {"event": "job", "data": json.dumps(payload)}
                await asyncio.sleep(0.75)
        except asyncio.CancelledError:
            return

    import json

    return EventSourceResponse(gen())


@ws_router.websocket("/ws/jobs/{id}")
async def ws_job(websocket: WebSocket, id: str):
    await websocket.accept()
    s = get_settings()
    allow_legacy = bool(getattr(s, "allow_legacy_token_login", False))

    # Authenticate:
    # - Prefer headers/cookies (mobile-safe)
    # - Allow legacy ?token= only when explicitly enabled AND peer is private (unsafe on public networks)
    auth_store: AuthStore | None = getattr(websocket.app.state, "auth_store", None)
    if auth_store is None:
        await websocket.close(code=1011)
        return
    ok = False
    ident: Identity | None = None
    try:
        token = ""
        # 1) Authorization header bearer
        auth = websocket.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
        # 2) X-Api-Key header (optional)
        if not token and bool(getattr(s, "enable_api_keys", True)):
            token = (websocket.headers.get("x-api-key") or "").strip()
        # 3) Signed session cookie (web UI mode)
        if not token:
            cookie = websocket.headers.get("cookie") or ""
            sess = ""
            for part in cookie.split(";"):
                if "=" not in part:
                    continue
                k, v = part.split("=", 1)
                if k.strip() == "session":
                    sess = v.strip()
                    break
            if sess:
                try:
                    from itsdangerous import BadSignature, URLSafeTimedSerializer  # type: ignore

                    ser = URLSafeTimedSerializer(
                        s.session_secret.get_secret_value(), salt="session"
                    )
                    token = str(ser.loads(sess, max_age=60 * 60 * 24 * 7))
                except BadSignature:
                    token = ""
        # 4) Legacy query token (unsafe) - gated
        if not token and allow_legacy:
            try:
                peer = websocket.client.host if websocket.client else ""
                client_ip = get_client_ip_from_headers(peer_ip=peer, headers=websocket.headers)
                ip = ipaddress.ip_address(client_ip) if client_ip else None
                if ip and (ip.is_private or ip.is_loopback):
                    token = websocket.query_params.get("token") or ""
            except Exception:
                token = ""

        if not token:
            ok = False
        elif token.startswith("dp_") and bool(getattr(s, "enable_api_keys", True)):
            parts = token.split("_", 2)
            if len(parts) == 3:
                _, prefix, _ = parts
                for k in auth_store.find_api_keys_by_prefix(prefix):
                    if verify_secret(k.key_hash, token):
                        scopes = set(k.scopes or [])
                        if "admin:*" in scopes or "read:job" in scopes:
                            user = auth_store.get_user(k.user_id)
                            if user is not None:
                                ident = Identity(
                                    kind="api_key",
                                    user=user,
                                    scopes=k.scopes,
                                    api_key_prefix=prefix,
                                )
                                ok = True
                        break
        else:
            data = decode_token(token, expected_typ="access")
            scopes = data.get("scopes") if isinstance(data.get("scopes"), list) else []
            scopes = {str(s) for s in scopes}
            if "admin:*" in scopes or "read:job" in scopes:
                sub = str(data.get("sub") or "")
                user = auth_store.get_user(sub)
                if user is not None:
                    ident = Identity(kind="user", user=user, scopes=[str(x) for x in scopes])
                    ok = True
    except Exception:
        ok = False

    if not ok or ident is None:
        await websocket.close(code=1008)
        return

    try:
        policy.require_invite_only(user=ident.user)
    except HTTPException:
        await websocket.close(code=1008)
        return

    store = getattr(websocket.app.state, "job_store", None)
    if store is None:
        await websocket.close(code=1011)
        return

    try:
        require_job_access(store=store, ident=ident, job_id=id)
    except HTTPException:
        await websocket.send_json({"error": "forbidden"})
        await websocket.close(code=1008)
        return

    last_updated = None
    try:
        while True:
            job = store.get(id)
            if job is None:
                await websocket.send_json({"error": "not_found"})
                await websocket.close()
                return

            if job.updated_at != last_updated:
                last_updated = job.updated_at
                await websocket.send_json(
                    {
                        "id": job.id,
                        "state": job.state,
                        "progress": job.progress,
                        "message": job.message,
                        "updated_at": job.updated_at,
                    }
                )

            if job.state in {JobState.DONE, JobState.FAILED, JobState.CANCELED}:
                await asyncio.sleep(0.2)
                return

            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        return


@router.get("/events/jobs/{id}")
async def sse_job(request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))):
    store = _get_store(request)

    async def gen():
        last_updated = None
        try:
            while True:
                if lifecycle.is_draining():
                    return
                if await request.is_disconnected():
                    return
                job = store.get(id)
                if job is None:
                    yield {"event": "message", "data": '{"error":"not_found"}'}
                    return
                try:
                    require_job_access(store=store, ident=ident, job=job)
                except HTTPException as ex:
                    if ex.status_code == 403:
                        yield {"event": "message", "data": '{"error":"forbidden"}'}
                        return
                    raise
                if job.updated_at != last_updated:
                    last_updated = job.updated_at
                    data = {
                        "id": job.id,
                        "state": job.state,
                        "progress": job.progress,
                        "message": job.message,
                        "updated_at": job.updated_at,
                    }
                    yield {"event": "message", "data": json.dumps(data)}
                if job.state in {JobState.DONE, JobState.FAILED, JobState.CANCELED}:
                    return
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return

    import json

    return EventSourceResponse(gen())
