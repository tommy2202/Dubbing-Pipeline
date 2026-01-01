from __future__ import annotations

import mimetypes
import os
import re
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.templating import Jinja2Templates

from anime_v2.utils.config import get_settings
from anime_v2.utils.log import logger

app = FastAPI(title="anime_v2 web")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
OUTPUT_ROOT = Path(os.environ.get("ANIME_V2_OUTPUT_DIR", str(Path.cwd() / "Output"))).resolve()


def _expected_token() -> str:
    return get_settings().api_token


def _get_token_from_request(request: Request) -> str | None:
    t = request.query_params.get("token")
    if t:
        return t
    c = request.cookies.get("auth")
    if c:
        return c
    return None


def require_auth(request: Request) -> str:
    token = _get_token_from_request(request)
    expected = _expected_token()
    if not token or token != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return token


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
                out.append({"job": key, "name": key})
            except Exception:
                continue
    out.sort(key=lambda x: x["name"])
    return out


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
def login(token: str, request: Request) -> Response:
    if token != _expected_token():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    resp = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    resp.set_cookie("auth", token, httponly=True, samesite="lax")
    return resp


@app.get("/", response_class=HTMLResponse)
def index(request: Request, token: str = Depends(require_auth)) -> HTMLResponse:
    videos = _iter_videos()
    return TEMPLATES.TemplateResponse(
        "index.html",
        {
            "request": request,
            "token": token,
            "videos": videos,
        },
    )


@app.get("/video/{job:path}")
def video(job: str, request: Request, token: str = Depends(require_auth)) -> Response:
    # Resolve and prevent traversal
    try:
        target = (OUTPUT_ROOT / job).resolve()
        target.relative_to(OUTPUT_ROOT)
    except Exception:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

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

