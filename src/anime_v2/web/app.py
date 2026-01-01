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

from anime_v2.jobs.queue import JobQueue
from anime_v2.jobs.store import JobStore
from anime_v2.utils.log import logger
from anime_v2.utils.security import verify_api_key
from anime_v2.web.routes_jobs import router as jobs_router

OUTPUT_ROOT = Path(os.environ.get("ANIME_V2_OUTPUT_DIR", str(Path.cwd() / "Output"))).resolve()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    store = JobStore(OUTPUT_ROOT / "jobs.db")
    q = JobQueue(store, concurrency=int(os.environ.get("JOBS_CONCURRENCY", "1")))
    app.state.job_store = store
    app.state.job_queue = q
    await q.start()
    yield
    await q.stop()


app = FastAPI(title="anime_v2 web", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

JOB_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

app.include_router(jobs_router)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    t0 = time.perf_counter()
    ip = request.client.host if request.client else "unknown"
    path = request.url.path
    try:
        response = await call_next(request)
    finally:
        dt_ms = (time.perf_counter() - t0) * 1000.0
        # Never log tokens: only log method + path (no query string).
        status_code = getattr(locals().get("response"), "status_code", 0)
        logger.info("http ip=%s %s %s %s %.1fms", ip, request.method, path, status_code, dt_ms)
    return response


def _iter_videos() -> list[dict]:
    """
    List dubbed videos from Output/*/*.mkv (plus a couple pragmatic fallbacks).
    """
    out = []
    patterns = [
        OUTPUT_ROOT.glob("*/*.mkv"),
        OUTPUT_ROOT.glob("*/*.mp4"),
        OUTPUT_ROOT.glob("*.dub.mkv"),
        OUTPUT_ROOT.glob("*.mkv"),
        OUTPUT_ROOT.glob("*.mp4"),
    ]
    seen = set()
    for it in patterns:
        for p in it:
            try:
                if not p.is_file():
                    continue
                rel = p.resolve().relative_to(OUTPUT_ROOT)
                key = str(rel)
                if key in seen:
                    continue
                seen.add(key)
                job = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
                out.append({"job": job, "name": key, "rel": key})
            except Exception:
                continue
    out.sort(key=lambda x: x["name"])
    return out


def _resolve_job(job: str) -> Path:
    """
    Map a safe job ID to a file path under OUTPUT_ROOT.
    Rebuild mapping from current output directory.
    """
    # Allow job UUIDs (resolve via job store) or hashed IDs (resolve via output listing).
    if UUID_RE.match(job):
        store = getattr(app.state, "job_store", None)
        if store is not None:
            j = store.get(job)
            if j is not None and j.output_mkv:
                p = Path(j.output_mkv)
                if p.exists() and p.is_file():
                    return p
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    if not JOB_RE.match(job):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    for v in _iter_videos():
        if v["job"] == job:
            try:
                target = (OUTPUT_ROOT / v["rel"]).resolve()
                target.relative_to(OUTPUT_ROOT)
                return target
            except Exception:
                break
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _range_to_slice(range_header: str, file_size: int) -> tuple[int, int]:
    """
    Return (start, end_inclusive).
    Supports:
      - bytes=START-
      - bytes=START-END
      - bytes=-SUFFIX (last SUFFIX bytes)
    """
    m = _RANGE_RE.match(range_header.strip())
    if not m:
        raise ValueError("invalid range")
    start_s, end_s = m.group(1), m.group(2)

    if start_s == "" and end_s == "":
        raise ValueError("invalid range")

    if start_s == "":
        # suffix
        suffix = int(end_s)
        if suffix <= 0:
            raise ValueError("invalid suffix")
        start = max(0, file_size - suffix)
        end = file_size - 1
        return start, end

    start = int(start_s)
    if start < 0 or start >= file_size:
        raise ValueError("start out of bounds")

    if end_s == "":
        end = file_size - 1
    else:
        end = int(end_s)
        if end < start:
            raise ValueError("end before start")
        end = min(end, file_size - 1)
    return start, end


def _file_iterator(path: Path, start: int, end_inclusive: int, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    with path.open("rb") as f:
        f.seek(start)
        remaining = end_inclusive - start + 1
        while remaining > 0:
            data = f.read(min(chunk_size, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


@app.get("/health")
def health() -> dict[str, str]:
    logger.info("[v2] health check")
    return {"status": "ok"}


@app.get("/login")
def login(request: Request) -> Response:
    # Uses the same token mechanism (?token=...).
    verify_api_key(request)
    resp = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    # Set cookie from query token; do not log it.
    token = request.query_params.get("token")
    if token:
        resp.set_cookie("auth", token, httponly=True, samesite="lax")
    return resp


@app.get("/", response_class=HTMLResponse)
def index(request: Request, _auth: None = Depends(verify_api_key)) -> HTMLResponse:
    videos = [{"job": v["job"], "name": v["name"]} for v in _iter_videos()]
    return TEMPLATES.TemplateResponse(
        "index.html",
        {
            "request": request,
            "videos": videos,
        },
    )


@app.get("/video/{job}")
def video(job: str, request: Request, _auth: None = Depends(verify_api_key)) -> Response:
    target = _resolve_job(job)

    file_size = target.stat().st_size
    content_type, _ = mimetypes.guess_type(str(target))
    if target.suffix.lower() == ".mkv":
        content_type = content_type or "video/x-matroska"
    else:
        content_type = content_type or "application/octet-stream"

    range_header = request.headers.get("range")
    headers = {"Accept-Ranges": "bytes"}

    if not range_header:
        return StreamingResponse(_file_iterator(target, 0, file_size - 1), media_type=content_type, headers=headers)

    try:
        start, end = _range_to_slice(range_header, file_size)
    except Exception:
        raise HTTPException(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, detail="Invalid Range")

    headers.update(
        {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(end - start + 1),
        }
    )
    return StreamingResponse(
        _file_iterator(target, start, end),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=content_type,
        headers=headers,
    )

