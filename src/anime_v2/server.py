from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import signal
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from anime_v2.api.deps import require_role, require_scope
from anime_v2.api.middleware import request_context_middleware
from anime_v2.api.models import AuthStore, Role, User, now_ts
from anime_v2.api.routes_auth import router as auth_router
from anime_v2.api.routes_audit import router as audit_router
from anime_v2.api.routes_keys import router as keys_router
from anime_v2.api.routes_runtime import router as runtime_router
from anime_v2.api.routes_settings import UserSettingsStore, router as settings_router
from anime_v2.config import get_settings
from anime_v2.jobs.queue import JobQueue
from anime_v2.jobs.store import JobStore
from anime_v2.ops import audit
from anime_v2.ops.metrics import REGISTRY
from anime_v2.ops.storage import periodic_prune_tick
from anime_v2.runtime.model_manager import ModelManager
from anime_v2.runtime.scheduler import Scheduler
from anime_v2.runtime import lifecycle
from anime_v2.utils.crypto import PasswordHasher, random_id
from anime_v2.utils.log import logger
from anime_v2.utils.ratelimit import RateLimiter
from anime_v2.utils.net import install_egress_policy
from anime_v2.web.routes_jobs import router as jobs_router
from anime_v2.web.routes_webrtc import router as webrtc_router
from anime_v2.web.routes_ui import router as ui_router

def _output_root() -> Path:
    return Path(os.environ.get("ANIME_V2_OUTPUT_DIR", str(Path.cwd() / "Output"))).resolve()


TEMPLATES_DIR = (Path(__file__).parent / "web" / "templates").resolve()
STATIC_DIR = (Path(__file__).parent / "web" / "static").resolve()
TEMPLATES = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure clean boot state (tests reuse the same process).
    try:
        lifecycle.end_draining()
    except Exception:
        pass
    # core pipeline stores
    out_root = _output_root()
    store = JobStore(out_root / "jobs.db")
    q = JobQueue(store, concurrency=int(os.environ.get("JOBS_CONCURRENCY", "1")))
    app.state.job_store = store
    app.state.job_queue = q
    app.state.output_root = out_root
    # runtime scheduler (in-proc)
    import asyncio as _asyncio

    loop = _asyncio.get_running_loop()

    def _enqueue_threadsafe(job: Job) -> None:
        # Runs in scheduler thread; forward to asyncio loop
        coro = q.enqueue(job)
        try:
            fut = _asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            try:
                coro.close()
            except Exception:
                pass
            raise
        fut.result(timeout=5.0)

    sched = Scheduler(store=store, enqueue_cb=_enqueue_threadsafe)
    Scheduler.install(sched)
    sched.start()
    app.state.scheduler = sched

    # auth store
    auth_store = AuthStore(out_root / "auth.db")
    app.state.auth_store = auth_store
    app.state.rate_limiter = RateLimiter()
    # per-user UI/API settings (stored on disk, default under ~/.anime_v2/settings.json)
    try:
        app.state.user_settings_store = UserSettingsStore()
    except Exception:
        app.state.user_settings_store = None

    # bootstrap admin user
    s = get_settings()
    install_egress_policy()
    audit.emit(
        "policy.egress",
        request_id=None,
        user_id=None,
        meta={
            "offline_mode": bool(s.offline_mode),
            "allow_egress": bool(s.allow_egress),
            "allow_hf_egress": bool(s.allow_hf_egress),
        },
    )
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
    # Periodic cleanup of stale work/ directories (best-effort).
    import asyncio as _asyncio2

    _prune_task: _asyncio2.Task | None = None

    async def _prune_loop() -> None:
        while True:
            try:
                periodic_prune_tick(output_root=OUTPUT_ROOT)
            except Exception as ex:
                logger.warning("workdir_prune_failed", error=str(ex))
            await _asyncio2.sleep(float(os.environ.get("WORK_PRUNE_INTERVAL_SEC", "3600")))

    try:
        # run one tick at boot, then start loop
        periodic_prune_tick(output_root=OUTPUT_ROOT)
        _prune_task = _asyncio2.create_task(_prune_loop())
    except Exception:
        _prune_task = None

    # Optional model pre-warm (env-controlled). Never prevent boot.
    try:
        ModelManager.instance().prewarm()
    except Exception as ex:
        logger.warning("model_prewarm_exception", error=str(ex))
    yield
    # Graceful drain on shutdown (or if signals have already initiated drain).
    lifecycle.begin_draining(timeout_sec=int(os.environ.get("DRAIN_TIMEOUT_SEC", "120")))
    try:
        sched.stop()
    except Exception:
        pass
    try:
        await q.graceful_shutdown(timeout_s=int(os.environ.get("DRAIN_TIMEOUT_SEC", "120")))
    finally:
        if _prune_task is not None:
            _prune_task.cancel()
        await q.stop()


app = FastAPI(title="anime_v2 server", lifespan=lifespan)
app.state.templates = TEMPLATES
try:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Signal handlers (best-effort, uvicorn will also trigger lifespan shutdown)
try:
    _sig_registered = False

    def _handle_term(signum, _frame=None):
        if not lifecycle.is_draining():
            lifecycle.begin_draining(timeout_sec=int(os.environ.get("DRAIN_TIMEOUT_SEC", "120")))

    for _sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(_sig, _handle_term)
    _sig_registered = True
except Exception:
    _sig_registered = False

# OpenTelemetry (opt-in via OTEL_EXPORTER_OTLP_ENDPOINT)
_otel_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
if _otel_endpoint:
    try:
        from opentelemetry import trace  # type: ignore
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # type: ignore
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # type: ignore
        from opentelemetry.sdk.resources import Resource  # type: ignore
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore

        resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "anime_v2")})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=_otel_endpoint)))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app)
        logger.info("otel_enabled", endpoint=_otel_endpoint)
    except Exception as ex:
        logger.warning("otel_enable_failed", error=str(ex))

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
# Also expose auth endpoints under /api/auth/* for browser-friendly UI wiring.
app.include_router(auth_router, prefix="/api")
app.include_router(audit_router)
app.include_router(keys_router)
app.include_router(runtime_router)
app.include_router(settings_router)
app.include_router(jobs_router)
app.include_router(webrtc_router)
app.include_router(ui_router)


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
        logger.info("http_done", ip=ip, method=request.method, path=path, status=status_code, duration_ms=dt_ms)
    return response


# Must be outermost so request_id is present for all logs (including log_requests).
app.middleware("http")(request_context_middleware)


def _iter_videos() -> list[dict]:
    OUTPUT_ROOT = _output_root()
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


def _safe_output_path(rel: str) -> Path:
    # only serve from OUTPUT_ROOT
    OUTPUT_ROOT = _output_root()
    if not rel:
        raise HTTPException(status_code=404, detail="Not found")
    rel = rel.lstrip("/").replace("\\", "/")
    p = (OUTPUT_ROOT / rel).resolve()
    try:
        p.relative_to(OUTPUT_ROOT)
    except Exception:
        raise HTTPException(status_code=404, detail="Not found")
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return p


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/healthz")
async def healthz():
    # Liveness: process is up.
    return {"ok": True}


@app.get("/metrics")
async def metrics():
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest  # type: ignore
    except Exception as ex:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"prometheus-client unavailable: {ex}")
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.get("/readyz")
async def readyz(request: Request):
    # Readiness: dependencies initialized and writable mounts present.
    try:
        if lifecycle.is_draining():
            raise RuntimeError("draining")
        store = getattr(request.app.state, "job_store", None)
        queue = getattr(request.app.state, "job_queue", None)
        auth_store = getattr(request.app.state, "auth_store", None)
        if store is None or queue is None or auth_store is None:
            raise RuntimeError("missing app state")
        # Output root should exist and be writable (read-only rootfs requires a mount here).
        out_root = getattr(request.app.state, "output_root", None) or _output_root()
        if not Path(out_root).exists():
            raise RuntimeError("Output dir missing")
        if not Path(out_root).is_dir():
            raise RuntimeError("Output is not a directory")
        if not os.access(str(out_root), os.W_OK):
            raise RuntimeError("Output not writable")
    except Exception as ex:
        raise HTTPException(status_code=503, detail=f"not ready: {ex}")
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, _: object = Depends(require_role(Role.viewer))):
    videos = _iter_videos()
    return TEMPLATES.TemplateResponse(request, "index.html", {"videos": videos})


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


@app.get("/files/{path:path}")
async def files(request: Request, path: str, _: object = Depends(require_scope("read:job"))):
    p = _safe_output_path(path)
    ctype, _ = mimetypes.guess_type(str(p))
    if not ctype:
        # HLS / TS
        if p.suffix.lower() == ".m3u8":
            ctype = "application/vnd.apple.mpegurl"
        elif p.suffix.lower() == ".ts":
            ctype = "video/mp2t"
        else:
            ctype = "application/octet-stream"

    gen, start, end, size = _range_stream(p, request.headers.get("range"))
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Content-Length": str(end - start + 1),
    }
    return StreamingResponse(gen, status_code=206, media_type=ctype, headers=headers)

