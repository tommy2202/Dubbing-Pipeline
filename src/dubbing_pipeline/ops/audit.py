from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.log import _redact_str  # type: ignore[attr-defined]

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
_AUDIT_PATH_KEYS = {
    "path",
    "paths",
    "file",
    "files",
    "filename",
    "video_path",
    "output_path",
    "log_path",
    "work_dir",
}
_AUDIT_LIST_KEYS = {"segments", "lines", "items", "updates"}


def _scrub_meta_safe(meta: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for kk, vv in meta.items():
        kks = str(kk)
        kl = kks.strip().lower()
        if kl in _AUDIT_TEXT_KEYS:
            if isinstance(vv, str):
                out[kks] = {"redacted": True, "len": len(vv)}
            else:
                out[kks] = {"redacted": True}
            continue
        if kl in _AUDIT_PATH_KEYS or "path" in kl or "file" in kl:
            if isinstance(vv, list):
                out[kks] = {"count": len(vv)}
            else:
                out[kks] = {"redacted": True}
            continue
        if kl in _AUDIT_LIST_KEYS and isinstance(vv, list):
            out[kks] = {"count": len(vv)}
            continue
        if isinstance(vv, str):
            if len(vv) > 200:
                out[kks] = {"redacted": True, "len": len(vv)}
            else:
                out[kks] = _redact_str(vv)
            continue
        if isinstance(vv, dict):
            out[kks] = {"keys": len(vv)}
            continue
        if isinstance(vv, list):
            out[kks] = {"count": len(vv)}
            continue
        out[kks] = vv
    return out


def _write_record(rec: dict[str, Any], *, job_id: str | None = None) -> None:
    ts = datetime.now(tz=timezone.utc)
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


def event(
    event_type: str,
    *,
    actor_id: str | None = None,
    resource_id: str | None = None,
    request_id: str | None = None,
    outcome: str | None = None,
    meta_safe: dict[str, Any] | None = None,
    job_id: str | None = None,
) -> None:
    """
    Coarse audit event: no content payloads or full filenames.
    """
    ts = datetime.now(tz=timezone.utc)
    rec: dict[str, Any] = {
        "ts": ts.isoformat(),
        "event": str(event_type),
        "event_type": str(event_type),
        "outcome": str(outcome or "unknown"),
    }
    if request_id:
        rec["request_id"] = request_id
    if actor_id:
        rec["actor_id"] = actor_id
        rec["user_id"] = actor_id  # backwards-compatible field
    if resource_id:
        rec["resource_id"] = resource_id
    if meta_safe:
        rec["meta"] = _scrub_meta_safe(meta_safe)
    _write_record(rec, job_id=job_id or resource_id)


def emit(
    event_type: str,
    *,
    request_id: str | None = None,
    user_id: str | None = None,
    meta: dict[str, Any] | None = None,
    job_id: str | None = None,
    outcome: str | None = None,
) -> None:
    """
    Append-only audit log (newline-delimited JSON), daily rotated by date.
    """
    resource_id = None
    if job_id:
        resource_id = str(job_id)
    if not resource_id and isinstance(meta, dict):
        for cand in ("resource_id", "job_id"):
            if meta.get(cand):
                resource_id = str(meta.get(cand))
                break
    event(
        event_type,
        actor_id=user_id,
        resource_id=resource_id,
        request_id=request_id,
        outcome=outcome,
        meta_safe=meta or None,
        job_id=job_id,
    )
