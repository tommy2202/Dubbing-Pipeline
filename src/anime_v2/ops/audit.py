from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from anime_v2.config import get_settings
from anime_v2.utils.log import _redact_str  # type: ignore[attr-defined]

_lock = Lock()


def _audit_dir() -> Path:
    return Path(get_settings().log_dir)


def _audit_path(ts: datetime) -> Path:
    return _audit_dir() / f"audit-{ts:%Y%m%d}.log"


def _audit_path_latest() -> Path:
    return _audit_dir() / "audit.jsonl"


def _job_audit_path(job_id: str) -> Path | None:
    job_id = str(job_id or "").strip()
    if not job_id:
        return None
    try:
        out_root = Path(get_settings().output_dir).resolve()
        # Stable per-job location independent of Output/<stem>/ naming:
        # Output/jobs/<job_id>/logs/audit.jsonl
        p = (out_root / "jobs" / job_id / "logs" / "audit.jsonl").resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:
        return None


_AUDIT_TEXT_KEYS = {"text", "subtitle", "subtitles", "transcript", "content", "prompt"}


def emit(
    event: str,
    *,
    request_id: str | None = None,
    user_id: str | None = None,
    meta: dict[str, Any] | None = None,
    job_id: str | None = None,
) -> None:
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

    # Determine per-job audit routing (prefer explicit job_id, else meta.job_id)
    if not job_id:
        try:
            if isinstance(meta, dict) and meta.get("job_id"):
                job_id = str(meta.get("job_id"))
        except Exception:
            job_id = None

    # Redact string fields defensively
    def _scrub(v: Any) -> Any:
        if isinstance(v, str):
            return _redact_str(v)
        if isinstance(v, dict):
            out: dict[str, Any] = {}
            for kk, vv in v.items():
                kks = str(kk)
                # Never store raw subtitle/transcript-like text in audit logs.
                if kks.lower() in _AUDIT_TEXT_KEYS and isinstance(vv, str):
                    out[kks] = {"redacted": True, "len": len(vv)}
                    continue
                # If a list/dict is suspiciously large, keep only a count.
                if kks.lower() in {"segments", "lines", "items", "updates"} and isinstance(
                    vv, list
                ):
                    out[kks] = {"count": len(vv)}
                    continue
                out[kks] = _scrub(vv)
            return out
        if isinstance(v, list):
            return [_scrub(x) for x in v]
        return v

    rec = _scrub(rec)

    daily = _audit_path(ts)
    latest = _audit_path_latest()
    daily.parent.mkdir(parents=True, exist_ok=True)
    latest.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))

    job_path = _job_audit_path(job_id) if job_id else None
    with _lock:
        with daily.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        with latest.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if job_path is not None:
            with job_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
