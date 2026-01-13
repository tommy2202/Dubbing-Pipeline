from __future__ import annotations

import json
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from anime_v2.config import get_settings
from anime_v2.jobs.models import Job
from anime_v2.utils.io import atomic_write_text, read_json
from anime_v2.utils.log import logger


def _output_root() -> Path:
    return Path(get_settings().output_dir).resolve()


def _url_for_path(path: Path) -> str | None:
    """
    Convert a filesystem path into a /files/* URL when it is under Output/.
    """
    out = _output_root()
    try:
        rel = str(Path(path).resolve().relative_to(out)).replace("\\", "/")
    except Exception:
        return None
    return f"/files/{rel}"


def read_manifest(path: Path) -> dict[str, Any] | None:
    data = read_json(Path(path), default=None)
    return data if isinstance(data, dict) else None


def write_manifest(
    *,
    job: Job,
    outputs: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> Path:
    """
    Canonical manifest writer (single source of truth).

    This must be the ONLY function that writes the library-facing manifest.json.
    Callers should treat it as best-effort and handle exceptions.
    """
    library_dir = Path(outputs["library_dir"]).resolve()
    library_dir.mkdir(parents=True, exist_ok=True)
    path = library_dir / "manifest.json"

    master = Path(outputs["master"]) if outputs.get("master") else None
    mobile = Path(outputs["mobile"]) if outputs.get("mobile") else None
    hls_index = Path(outputs["hls_index"]) if outputs.get("hls_index") else None
    logs_dir = Path(outputs["logs_dir"]) if outputs.get("logs_dir") else None
    qa_dir = Path(outputs["qa_dir"]) if outputs.get("qa_dir") else None

    owner_user_id = str(getattr(job, "owner_id", "") or getattr(job, "owner_user_id", "") or "")

    def _p(p: Path | None) -> str | None:
        return str(p.resolve()) if p is not None else None

    def _u(p: Path | None) -> str | None:
        return _url_for_path(p) if p is not None else None

    created_at = str(getattr(job, "created_at", "") or "")
    if not created_at:
        created_at = str(outputs.get("created_at") or "")
    if not created_at:
        created_at = str(time.time())

    status = ""
    with suppress(Exception):
        st = getattr(job, "state", None)
        status = st.value if hasattr(st, "value") else str(st or "")

    payload: dict[str, Any] = {
        "version": 1,
        "job_id": str(job.id),
        "created_at": created_at,
        "status": status,
        "mode": str(getattr(job, "mode", "") or ""),
        "series_title": str(getattr(job, "series_title", "") or ""),
        "series_slug": str(getattr(job, "series_slug", "") or ""),
        "season_number": int(getattr(job, "season_number", 0) or 0),
        "episode_number": int(getattr(job, "episode_number", 0) or 0),
        "owner_user_id": owner_user_id,
        "visibility": str(getattr(job, "visibility", "private") or "private").split(".", 1)[-1],
        "paths": {
            "library_dir": _p(library_dir),
            "master": _p(master),
            "mobile": _p(mobile),
            "hls_index": _p(hls_index),
            "logs_dir": _p(logs_dir),
            "qa_dir": _p(qa_dir),
        },
        "urls": {
            "master": _u(master),
            "mobile": _u(mobile),
            "hls_index": _u(hls_index),
            "logs_dir": _u(logs_dir),
            "qa_dir": _u(qa_dir),
        },
    }
    if extra:
        payload["extra"] = dict(extra)

    # Atomic write to avoid partial manifests.
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    logger.info("library_manifest_written", job_id=str(job.id), path=str(path))
    return path

