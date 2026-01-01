from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.templating import Jinja2Templates

from anime_v2.api.deps import require_role, require_scope
from anime_v2.api.models import AuthStore, Role, User, now_ts
from anime_v2.api.routes_auth import router as auth_router
from anime_v2.api.routes_keys import router as keys_router
from anime_v2.config import get_settings
from anime_v2.jobs.queue import JobQueue
from anime_v2.jobs.store import JobStore
from anime_v2.utils.crypto import PasswordHasher, random_id
from anime_v2.utils.log import logger
from anime_v2.utils.ratelimit import RateLimiter
from anime_v2.utils.net import install_egress_policy
from anime_v2.web.routes_jobs import router as jobs_router
from anime_v2.web.routes_webrtc import router as webrtc_router

OUTPUT_ROOT = Path(os.environ.get("ANIME_V2_OUTPUT_DIR", str(Path.cwd() / "Output"))).resolve()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # core pipeline stores
    store = JobStore(OUTPUT_ROOT / "jobs.db")
    q = JobQueue(store, concurrency=int(os.environ.get("JOBS_CONCURRENCY", "1")))
    app.state.job_store = store
    app.state.job_queue = q

    # auth store
    auth_store = AuthStore(OUTPUT_ROOT / "auth.db")
    app.state.auth_store = auth_store
    app.state.rate_limiter = RateLimiter()

    # bootstrap admin user
    s = get_settings()
    install_egress_policy()
    if s.admin_username and s.admin_password:
        ph = PasswordHasher()
        u = User(
            id=random_id("u_", 16),
            username=s.admin_username,
            password_hash=ph.hash(s.admin_password.get_secret_value()),
            role=Role.admin,
            totp_secret=None,
            totp_enabled=False,
            created_at=now_ts(),
        )
        try:
            auth_store.upsert_user(u)
        except Exception as ex:
            logger.warning("admin bootstrap failed (%s)", ex)

    await q.start()
    yield
    await q.stop()


app = FastAPI(title="anime_v2 server", lifespan=lifespan)

# Strict CORS: only configured origins, credentials on for cookies
s = get_settings()
allow_origins = s.cors_origin_list()
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "X-Api-Key"],
)

app.include_router(auth_router)
app.include_router(keys_router)
app.include_router(jobs_router)
app.include_router(webrtc_router)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    t0 = time.perf_counter()
    ip = request.client.host if request.client else "unknown"
    path = request.url.path
    try:
        response = await call_next(request)
    finally:
        dt_ms = (time.perf_counter() - t0) * 1000.0
        status_code = getattr(locals().get("response"), "status_code", 0)
        logger.info("http ip=%s %s %s %s %.1fms", ip, request.method, path, status_code, dt_ms)
    return response


def _iter_videos() -> list[dict]:
    out = []
    patterns = [
        OUTPUT_ROOT.glob("*/*.dub.mkv"),
        OUTPUT_ROOT.glob("*/*.dub.mp4"),
        OUTPUT_ROOT.glob("*/*.mkv"),
        OUTPUT_ROOT.glob("*/*.mp4"),
    ]
    seen = set()
    for it in patterns:
        for p in it:
            try:
                rp = p.resolve()
            except Exception:
                continue
            if rp in seen:
                continue
            seen.add(rp)
            rel = str(rp.relative_to(OUTPUT_ROOT)).replace("\\", "/")
            job = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:32]
            out.append({"job": job, "name": rel, "path": rp})
    out.sort(key=lambda x: x["name"])
    return out


def _resolve_job(job: str) -> Path:
    # Only allow hashed ids emitted by _iter_videos
    vids = _iter_videos()
    for v in vids:
        if v["job"] == job:
            return Path(v["path"])
    raise HTTPException(status_code=404, detail="Not found")


def _range_stream(path: Path, range_header: str | None):
    size = path.stat().st_size
    start = 0
    end = size - 1
    if range_header and range_header.startswith("bytes="):
        r = range_header.replace("bytes=", "", 1)
        if "-" in r:
            a, b = r.split("-", 1)
            if a.strip():
                start = int(a)
            if b.strip():
                end = int(b)
    start = max(0, min(start, size - 1))
    end = max(start, min(end, size - 1))
    length = end - start + 1

    def gen() -> Iterator[bytes]:
        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return gen(), start, end, size


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, _: object = Depends(require_role(Role.viewer))):
    videos = _iter_videos()
    return TEMPLATES.TemplateResponse("index.html", {"request": request, "videos": videos})


@app.get("/video/{job}")
async def video(request: Request, job: str, _: object = Depends(require_scope("read:job"))):
    p = _resolve_job(job)
    ctype, _ = mimetypes.guess_type(str(p))
    ctype = ctype or ("video/mp4" if p.suffix.lower() == ".mp4" else "video/x-matroska")

    gen, start, end, size = _range_stream(p, request.headers.get("range"))
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Content-Length": str(end - start + 1),
    }
    return StreamingResponse(gen, status_code=206, media_type=ctype, headers=headers)

