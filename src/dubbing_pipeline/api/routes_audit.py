from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request

from dubbing_pipeline.api.deps import Identity, current_identity
from dubbing_pipeline.config import get_settings


def _audit_dir() -> Path:
    return Path(get_settings().log_dir).resolve()


def _audit_path_today() -> Path:
    ts = datetime.now(tz=timezone.utc)
    return _audit_dir() / f"audit-{ts:%Y%m%d}.log"


def _tail_lines(path: Path, *, max_lines: int = 500, max_bytes: int = 512 * 1024) -> list[str]:
    """
    Best-effort tail of a text file, reading from the end.
    """
    if not path.exists() or not path.is_file():
        return []
    size = path.stat().st_size
    if size <= 0:
        return []
    read_size = min(int(max_bytes), int(size))
    try:
        with path.open("rb") as f:
            f.seek(max(0, size - read_size))
            data = f.read(read_size)
    except Exception:
        return []
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # If we started mid-line, drop the first partial line unless we read the whole file.
    if size > read_size and lines:
        lines = lines[1:]
    return lines[-int(max_lines) :]


router = APIRouter(tags=["audit"])


@router.get("/api/audit/recent")
async def audit_recent(
    request: Request,
    limit: int = 50,
    ident: Identity = Depends(current_identity),
) -> dict[str, Any]:
    lim = max(1, min(200, int(limit)))
    # Current day only (simple + fast). If file missing, return empty.
    path = _audit_path_today()
    raw = _tail_lines(path, max_lines=2000)
    items: list[dict[str, Any]] = []
    uid = str(ident.user.id)
    for ln in raw:
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        if str(rec.get("user_id") or "") != uid:
            continue
        # minimal, stable payload
        items.append(
            {
                "ts": rec.get("ts"),
                "event": rec.get("event"),
                "request_id": rec.get("request_id"),
                "meta": rec.get("meta") if isinstance(rec.get("meta"), dict) else None,
            }
        )
    items = items[-lim:]
    return {"items": items, "limit": lim}
