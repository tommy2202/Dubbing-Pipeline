from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from dubbing_pipeline.api.access import require_file_access, require_job_access
from dubbing_pipeline.api.deps import Identity, require_scope
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import JobState
from dubbing_pipeline.utils.log import logger
from dubbing_pipeline.utils.ratelimit import RateLimiter

router = APIRouter()


def _get_store(request: Request):
    store = getattr(request.app.state, "job_store", None)
    if store is None:
        raise HTTPException(status_code=500, detail="Job store not initialized")
    return store


def _ice_servers() -> list[dict]:
    s = get_settings()
    stun = str(s.webrtc_stun).strip()
    servers: list[dict] = []
    if stun:
        servers.append({"urls": [u.strip() for u in stun.split(",") if u.strip()]})
    turn_url = s.turn_url
    turn_user = s.turn_username
    turn_pass = s.turn_password
    # Avoid leaking TURN secrets by default; only include if explicitly enabled.
    if (
        bool(getattr(s, "webrtc_expose_turn_credentials", False))
        and turn_url
        and turn_user
        and turn_pass
    ):
        servers.append({"urls": [turn_url], "username": turn_user, "credential": turn_pass})
    return servers


def _get_rl(request: Request) -> RateLimiter:
    rl = getattr(request.app.state, "rate_limiter", None)
    if rl is None:
        rl = RateLimiter()
        request.app.state.rate_limiter = rl
    return rl


def _resolve_job_media_path(
    request: Request,
    *,
    job_id: str,
    ident: Identity,
    video_path: str | None = None,
) -> Path:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=job_id)

    # Prefer explicit video_path if provided (still must exist).
    if video_path:
        p = Path(video_path).expanduser().resolve()
        if not p.exists() or not p.is_file():
            raise HTTPException(status_code=404, detail="video_path not found")
        require_file_access(store=store, ident=ident, path=p)
        return p
    if job.state != JobState.DONE or not job.output_mkv:
        raise HTTPException(status_code=409, detail="Job not done")
    p = Path(job.output_mkv)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Output file missing")
    return p.resolve()


@dataclass
class _Peer:
    pc: object
    player: object | None
    created_at: float
    last_activity: float
    ip: str


_peers: dict[str, _Peer] = {}
_peers_lock = asyncio.Lock()
_idle_watch_tasks: dict[str, asyncio.Task] = {}


def _idle_timeout_s() -> int:
    return int(get_settings().webrtc_idle_timeout_s)


def _max_pcs_per_ip() -> int:
    return int(get_settings().webrtc_max_pcs_per_ip)


async def _close_peer(token: str, reason: str) -> None:
    async with _peers_lock:
        peer = _peers.pop(token, None)
    if peer is None:
        return
    try:
        pc = peer.pc
        await pc.close()
    except Exception:
        pass
    try:
        if peer.player is not None:
            peer.player.stop()
    except Exception:
        pass
    logger.info("webrtc peer closed token=%s reason=%s ip=%s", token, reason, peer.ip)


async def _idle_watch(token: str) -> None:
    try:
        while True:
            await asyncio.sleep(10)
            async with _peers_lock:
                peer = _peers.get(token)
                if peer is None:
                    return
                idle = time.monotonic() - peer.last_activity
            if idle > _idle_timeout_s():
                await _close_peer(token, "idle_timeout")
                return
    except asyncio.CancelledError:
        logger.info("task stopped", task="webrtc.idle_watch", token=str(token))
        return
    finally:
        async with _peers_lock:
            _idle_watch_tasks.pop(token, None)


async def shutdown_webrtc_peers() -> None:
    async with _peers_lock:
        tokens = list(_peers.keys())
        idle_tasks = list(_idle_watch_tasks.values())
        _idle_watch_tasks.clear()
    for t in idle_tasks:
        t.cancel()
    if idle_tasks:
        await asyncio.gather(*idle_tasks, return_exceptions=True)
    for token in tokens:
        await _close_peer(token, "shutdown")


@router.post("/webrtc/offer")
async def webrtc_offer(
    request: Request, ident: Identity = Depends(require_scope("read:job"))
) -> dict:
    # auth required (cookie/bearer/api-key), CSRF enforced by require_scope for cookie flows.

    # Lazy import so local installs don't break if aiortc/av aren't installed.
    try:
        from aiortc import RTCPeerConnection, RTCSessionDescription  # type: ignore
        from aiortc.contrib.media import MediaPlayer  # type: ignore
    except Exception as ex:
        raise HTTPException(status_code=503, detail=f"WebRTC deps not installed: {ex}") from ex

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    job_id = str(body.get("job_id") or "")
    sdp = body.get("sdp")
    typ = body.get("type")
    video_path = body.get("video_path")
    if not job_id or not isinstance(sdp, str) or not isinstance(typ, str):
        raise HTTPException(status_code=400, detail="Required: job_id, sdp, type")

    media_path = _resolve_job_media_path(
        request, job_id=job_id, ident=ident, video_path=str(video_path) if video_path else None
    )

    ip = request.client.host if request.client else "unknown"
    # Rate limit offers (per IP) to avoid resource exhaustion.
    rl = _get_rl(request)
    if not rl.allow(f"webrtc:offer:ip:{ip}", limit=10, per_seconds=60):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    async with _peers_lock:
        active_for_ip = sum(1 for p in _peers.values() if p.ip == ip)
        if active_for_ip >= _max_pcs_per_ip():
            raise HTTPException(status_code=429, detail="Too many peer connections for this IP")

    cfg = {"iceServers": _ice_servers()}
    pc = RTCPeerConnection(configuration=cfg)

    peer_token = uuid.uuid4().hex[:12]

    player = None
    try:
        # Low-latency-ish options. Since it's a file, this is "fast start" rather than true live.
        player = MediaPlayer(str(media_path), options={"fflags": "nobuffer"})

        # Add tracks if present
        if getattr(player, "video", None):
            pc.addTrack(player.video)
        if getattr(player, "audio", None):
            pc.addTrack(player.audio)

        now = time.monotonic()
        async with _peers_lock:
            _peers[peer_token] = _Peer(
                pc=pc, player=player, created_at=now, last_activity=now, ip=ip
            )

        def touch() -> None:
            # best-effort update
            async def _t():
                async with _peers_lock:
                    p = _peers.get(peer_token)
                    if p:
                        p.last_activity = time.monotonic()

            asyncio.create_task(_t())

        @pc.on("iceconnectionstatechange")
        async def on_ice_state_change():
            touch()
            logger.info("webrtc ice_state token=%s state=%s", peer_token, pc.iceConnectionState)
            if pc.iceConnectionState in ("failed", "closed"):
                await _close_peer(peer_token, f"ice_{pc.iceConnectionState}")

        @pc.on("connectionstatechange")
        async def on_conn_state_change():
            touch()
            logger.info("webrtc conn_state token=%s state=%s", peer_token, pc.connectionState)
            if pc.connectionState in ("failed", "closed", "disconnected"):
                await _close_peer(peer_token, f"conn_{pc.connectionState}")

        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=typ))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        idle_task = asyncio.create_task(_idle_watch(peer_token))
        async with _peers_lock:
            _idle_watch_tasks[peer_token] = idle_task
        logger.info(
            "webrtc peer created token=%s job_id=%s ip=%s file=%s",
            peer_token,
            job_id,
            ip,
            media_path.name,
        )

        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
            "peer_token": peer_token,
        }
    except HTTPException:
        raise
    except Exception as ex:
        with suppress(Exception):
            await pc.close()
        with suppress(Exception):
            if player is not None:
                player.stop()
        raise HTTPException(status_code=500, detail=f"WebRTC offer failed: {ex}") from ex


@router.get("/webrtc/demo", response_class=HTMLResponse)
async def webrtc_demo(request: Request) -> HTMLResponse:
    # Demo page is protected.
    ident = require_scope("read:job")(request)  # type: ignore[misc]
    store = _get_store(request)
    jobs = [j for j in store.list(limit=100) if j.state == JobState.DONE and j.output_mkv]
    visible = []
    for j in jobs:
        try:
            require_job_access(store=store, ident=ident, job=j)
        except HTTPException as ex:
            if ex.status_code == 403:
                continue
            raise
        visible.append(j)
    jobs = visible
    # Minimal page; token is supplied as query param by the user.
    ice = _ice_servers()
    ice_json = json.dumps(ice)

    opts = (
        "\n".join(
            [f'<option value="{j.id}">{j.id} — {Path(j.output_mkv).name}</option>' for j in jobs]
        )
        or '<option value="">(no done jobs)</option>'
    )

    html = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>WebRTC Preview</title>
    <style>
      body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 16px; }}
      video {{ width: 100%; max-width: 960px; background: #000; border-radius: 10px; }}
      select, button, input {{ padding: 10px; font-size: 16px; }}
      .muted {{ color: #666; font-size: 14px; }}
    </style>
  </head>
  <body>
    <h2>WebRTC Preview</h2>
    <div class="muted">
      This is a preview stream of a completed output file. For public internet/NAT traversal you usually need TURN.
    </div>

    <div style="margin-top:12px;">
      <label class="muted">Job</label><br/>
      <select id="job">{opts}</select>
      <button id="start">Start</button>
    </div>

    <div class="muted" style="margin-top:10px;">
      ICE servers are configured from server env (STUN by default; TURN optional).
    </div>

    <video id="v" autoplay playsinline controls></video>

    <pre id="log" class="muted"></pre>

    <script>
      const ICE = {ice_json};
      const params = new URLSearchParams(location.search);
      const token = params.get("token") || "";

      const log = (msg) => {{
        const el = document.getElementById("log");
        el.textContent = (el.textContent + msg + "\\n");
      }};

      document.getElementById("start").addEventListener("click", async () => {{
        const jobId = document.getElementById("job").value;
        if (!jobId) {{
          log("No job selected.");
          return;
        }}
        const pc = new RTCPeerConnection({{ iceServers: ICE }});
        pc.ontrack = (ev) => {{
          log("ontrack: " + ev.track.kind);
          const v = document.getElementById("v");
          v.srcObject = ev.streams[0];
        }};
        pc.oniceconnectionstatechange = () => log("ice=" + pc.iceConnectionState);
        pc.onconnectionstatechange = () => log("conn=" + pc.connectionState);

        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);

        const url = "/webrtc/offer" + (token ? ("?token=" + encodeURIComponent(token)) : "");
        const res = await fetch(url, {{
          method: "POST",
          headers: {{ "content-type": "application/json" }},
          body: JSON.stringify({{ job_id: jobId, sdp: offer.sdp, type: offer.type }})
        }});
        if (!res.ok) {{
          log("Offer failed: " + res.status);
          log(await res.text());
          return;
        }}
        const ans = await res.json();
        await pc.setRemoteDescription(ans);
        log("Started. peer_token=" + (ans.peer_token || ""));
      }});
    </script>
  </body>
</html>"""
    return HTMLResponse(html)
