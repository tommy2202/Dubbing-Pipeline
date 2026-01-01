from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from anime_v2.jobs.models import JobState
from anime_v2.utils.log import logger
from anime_v2.api.deps import require_scope

router = APIRouter()


def _get_store(request: Request):
    store = getattr(request.app.state, "job_store", None)
    if store is None:
        raise HTTPException(status_code=500, detail="Job store not initialized")
    return store


def _ice_servers() -> list[dict]:
    stun = os.environ.get("WEBRTC_STUN", "stun:stun.l.google.com:19302").strip()
    servers: list[dict] = []
    if stun:
        servers.append({"urls": [u.strip() for u in stun.split(",") if u.strip()]})
    turn_url = os.environ.get("TURN_URL")
    turn_user = os.environ.get("TURN_USERNAME")
    turn_pass = os.environ.get("TURN_PASSWORD")
    if turn_url and turn_user and turn_pass:
        servers.append({"urls": [turn_url], "username": turn_user, "credential": turn_pass})
    return servers


def _resolve_job_media_path(request: Request, job_id: str, video_path: str | None = None) -> Path:
    # Prefer explicit video_path if provided (still must exist).
    if video_path:
        p = Path(video_path).expanduser().resolve()
        if not p.exists() or not p.is_file():
            raise HTTPException(status_code=404, detail="video_path not found")
        return p

    store = _get_store(request)
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
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
_IDLE_TIMEOUT_S = int(os.environ.get("WEBRTC_IDLE_TIMEOUT_S", "300"))
_MAX_PCS_PER_IP = int(os.environ.get("WEBRTC_MAX_PCS_PER_IP", "2"))


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
    while True:
        await asyncio.sleep(10)
        async with _peers_lock:
            peer = _peers.get(token)
            if peer is None:
                return
            idle = time.monotonic() - peer.last_activity
        if idle > _IDLE_TIMEOUT_S:
            await _close_peer(token, "idle_timeout")
            return


@router.post("/webrtc/offer")
async def webrtc_offer(request: Request) -> dict:
    # read-only access required
    require_scope("read:job")(request)  # type: ignore[misc]

    # Lazy import so local installs don't break if aiortc/av aren't installed.
    try:
        from aiortc import RTCPeerConnection, RTCSessionDescription  # type: ignore
        from aiortc.contrib.media import MediaPlayer  # type: ignore
    except Exception as ex:
        raise HTTPException(status_code=503, detail=f"WebRTC deps not installed: {ex}")

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    job_id = str(body.get("job_id") or "")
    sdp = body.get("sdp")
    typ = body.get("type")
    video_path = body.get("video_path")
    if not job_id or not isinstance(sdp, str) or not isinstance(typ, str):
        raise HTTPException(status_code=400, detail="Required: job_id, sdp, type")

    media_path = _resolve_job_media_path(request, job_id=job_id, video_path=str(video_path) if video_path else None)

    ip = request.client.host if request.client else "unknown"

    async with _peers_lock:
        active_for_ip = sum(1 for p in _peers.values() if p.ip == ip)
        if active_for_ip >= _MAX_PCS_PER_IP:
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
            _peers[peer_token] = _Peer(pc=pc, player=player, created_at=now, last_activity=now, ip=ip)

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

        asyncio.create_task(_idle_watch(peer_token))
        logger.info("webrtc peer created token=%s job_id=%s ip=%s file=%s", peer_token, job_id, ip, media_path.name)

        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type, "peer_token": peer_token}
    except HTTPException:
        raise
    except Exception as ex:
        try:
            await pc.close()
        except Exception:
            pass
        try:
            if player is not None:
                player.stop()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"WebRTC offer failed: {ex}")


@router.get("/webrtc/demo", response_class=HTMLResponse)
async def webrtc_demo(request: Request) -> HTMLResponse:
    require_scope("read:job")(request)  # type: ignore[misc]
    store = _get_store(request)
    jobs = [j for j in store.list(limit=100) if j.state == JobState.DONE and j.output_mkv]
    # Minimal page; token is supplied as query param by the user.
    ice = _ice_servers()
    ice_json = json.dumps(ice)

    opts = "\n".join([f'<option value="{j.id}">{j.id} — {Path(j.output_mkv).name}</option>' for j in jobs]) or "<option value=\"\">(no done jobs)</option>"

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

