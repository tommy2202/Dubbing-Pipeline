from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from anime_v2.utils.log import _redact_str  # type: ignore[attr-defined]

_lock = Lock()


def _audit_dir() -> Path:
    return Path(os.environ.get("ANIME_V2_LOG_DIR", "logs"))


def _audit_path(ts: datetime) -> Path:
    return _audit_dir() / f"audit-{ts:%Y%m%d}.log"


def emit(event: str, *, request_id: str | None = None, user_id: str | None = None, meta: dict[str, Any] | None = None) -> None:
    """
    Append-only audit log (newline-delimited JSON), daily rotated by date.
    """
    ts = datetime.now(tz=UTC)
    rec: dict[str, Any] = {"ts": ts.isoformat(), "event": event}
    if request_id:
        rec["request_id"] = request_id
    if user_id:
        rec["user_id"] = user_id
    if meta:
        rec["meta"] = meta

    # Redact string fields defensively
    def _scrub(v: Any) -> Any:
        if isinstance(v, str):
            return _redact_str(v)
        if isinstance(v, dict):
            return {kk: _scrub(vv) for kk, vv in v.items()}
        if isinstance(v, list):
            return [_scrub(x) for x in v]
        return v

    rec = _scrub(rec)

    path = _audit_path(ts)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
    with _lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

