from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, normalize_visibility
from dubbing_pipeline.jobs.store import JobStore
from dubbing_pipeline.utils.io import atomic_write_text, read_json
from dubbing_pipeline.utils.log import logger


def _output_root() -> Path:
    return Path(get_settings().output_dir).resolve()


def registry_path(*, output_root: Path | None = None) -> Path:
    out = Path(output_root or _output_root()).resolve()
    state_dir = out / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "manifest_registry.json"


def read_registry(*, output_root: Path | None = None) -> dict[str, Any]:
    path = registry_path(output_root=output_root)
    data = read_json(path, default={})
    if not isinstance(data, dict):
        return {}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return {}
    return dict(entries)


def write_registry(entries: dict[str, Any], *, output_root: Path | None = None) -> Path:
    path = registry_path(output_root=output_root)
    payload = {"version": 1, "updated_at": time.time(), "entries": entries}
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _safe_under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except Exception:
        return False


def _manifest_priority(path: Path) -> int:
    parts = {p.lower() for p in path.parts}
    return 0 if "library" in parts else 1


def _entry_from_manifest(
    *, manifest_path: Path, manifest: dict[str, Any], job: Job | None
) -> dict[str, Any] | None:
    job_id = str(manifest.get("job_id") or "")
    if job is not None:
        job_id = str(job.id or job_id)
    job_id = str(job_id).strip()
    if not job_id:
        return None

    owner = ""
    if job is not None:
        owner = str(getattr(job, "owner_id", "") or "")
    if not owner:
        owner = str(manifest.get("owner_user_id") or "")

    series_title = (
        str(getattr(job, "series_title", "") or "")
        if job is not None
        else str(manifest.get("series_title") or "")
    )
    series_slug = (
        str(getattr(job, "series_slug", "") or "")
        if job is not None
        else str(manifest.get("series_slug") or "")
    )
    season = (
        int(getattr(job, "season_number", 0) or 0)
        if job is not None
        else int(manifest.get("season_number") or 0)
    )
    episode = (
        int(getattr(job, "episode_number", 0) or 0)
        if job is not None
        else int(manifest.get("episode_number") or 0)
    )

    raw_vis = ""
    if job is not None:
        raw_vis = str(getattr(job, "visibility", "") or "")
    if not raw_vis:
        raw_vis = str(manifest.get("visibility") or "")
    vis = normalize_visibility(raw_vis).value

    created_at = str(manifest.get("created_at") or "")
    updated_at = str(manifest.get("updated_at") or "")
    if not updated_at:
        updated_at = created_at

    return {
        "job_id": job_id,
        "manifest_path": str(manifest_path),
        "owner_user_id": owner,
        "series_title": series_title,
        "series_slug": series_slug,
        "season_number": int(season or 0),
        "episode_number": int(episode or 0),
        "visibility": vis,
        "created_at": created_at,
        "updated_at": updated_at,
        "priority": _manifest_priority(manifest_path),
    }


def register_manifest(
    *, job: Job, manifest_path: Path, output_root: Path | None = None
) -> Path | None:
    path = Path(manifest_path).resolve()
    out = Path(output_root or _output_root()).resolve()
    if path.is_symlink() or not _safe_under_root(path, out):
        logger.warning("manifest_registry_skip_unsafe", path=str(path))
        return None
    entries = read_registry(output_root=out)
    man = read_json(path, default=None)
    if not isinstance(man, dict):
        man = {"job_id": job.id}
    entry = _entry_from_manifest(manifest_path=path, manifest=man, job=job)
    if entry is None:
        return None
    entry.pop("priority", None)
    entries[str(job.id)] = entry
    return write_registry(entries, output_root=out)


def remove_manifest_entry(*, job_id: str, output_root: Path | None = None) -> None:
    jid = str(job_id or "").strip()
    if not jid:
        return
    out = Path(output_root or _output_root()).resolve()
    entries = read_registry(output_root=out)
    if jid in entries:
        entries.pop(jid, None)
        write_registry(entries, output_root=out)


def update_registry_from_manifest(
    path: Path, *, output_root: Path | None = None
) -> Path | None:
    manifest_path = Path(path).resolve()
    out = Path(output_root or _output_root()).resolve()
    if manifest_path.is_symlink() or not _safe_under_root(manifest_path, out):
        return None
    man = read_json(manifest_path, default=None)
    if not isinstance(man, dict):
        return None
    entry = _entry_from_manifest(manifest_path=manifest_path, manifest=man, job=None)
    if entry is None:
        return None
    entry.pop("priority", None)
    entries = read_registry(output_root=out)
    entries[str(entry.get("job_id") or "")] = entry
    return write_registry(entries, output_root=out)


def repair_manifest_registry(
    *,
    store: JobStore,
    output_root: Path | None = None,
    prefer_library: bool = True,
) -> dict[str, Any]:
    out = Path(output_root or _output_root()).resolve()
    entries: dict[str, Any] = {}
    scanned = 0
    skipped_orphan = 0
    for root, dirs, files in os.walk(str(out), followlinks=False):
        # Do not traverse symlinked dirs.
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        if "manifest.json" not in files:
            continue
        manifest_path = Path(root) / "manifest.json"
        if manifest_path.is_symlink():
            continue
        try:
            resolved = manifest_path.resolve()
            if not _safe_under_root(resolved, out):
                continue
        except Exception:
            continue
        man = read_json(manifest_path, default=None)
        if not isinstance(man, dict):
            continue
        scanned += 1
        job_id = str(man.get("job_id") or "").strip()
        if not job_id:
            logger.warning("manifest_registry_missing_job_id", path=str(manifest_path))
            continue
        job = store.get(job_id)
        if job is None:
            skipped_orphan += 1
            logger.warning("manifest_registry_orphan_job", job_id=job_id, path=str(manifest_path))
            continue
        entry = _entry_from_manifest(manifest_path=resolved, manifest=man, job=job)
        if entry is None:
            continue
        priority = int(entry.pop("priority", 1))
        cur = entries.get(job_id)
        if cur:
            cur_priority = int(cur.get("_priority", 9))
            if prefer_library and priority < cur_priority:
                entry["_priority"] = priority
                entries[job_id] = entry
        else:
            entry["_priority"] = priority
            entries[job_id] = entry

    # strip priority before writing
    for v in entries.values():
        v.pop("_priority", None)

    write_registry(entries, output_root=out)
    logger.info(
        "manifest_registry_repair_done",
        total_entries=len(entries),
        scanned=int(scanned),
        skipped_orphan=int(skipped_orphan),
    )
    return {"entries": entries, "scanned": scanned, "skipped_orphan": skipped_orphan}
