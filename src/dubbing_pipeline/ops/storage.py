from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from fastapi import HTTPException

from dubbing_pipeline.library.paths import get_job_output_root, get_library_root_for_job
from dubbing_pipeline.jobs.models import Job

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.log import logger


def ensure_free_space(*, min_gb: int, path: Path) -> None:
    """
    Raise 507 if free disk space is below min_gb.
    """
    # Test-friendly behavior:
    # - Many CI runners have tight `/tmp` quotas, and our tests often point OUTPUT_DIR at a tmp dir.
    # - If a test wants to exercise the free-space guard, it sets `MIN_FREE_GB` explicitly.
    if os.environ.get("PYTEST_CURRENT_TEST") and "MIN_FREE_GB" not in os.environ:
        return

    p = Path(path).resolve()
    usage = shutil.disk_usage(str(p))
    free_gb = usage.free / (1024**3)
    if free_gb < float(min_gb):
        raise HTTPException(
            status_code=507,
            detail=f"Insufficient storage: {free_gb:.1f}GB free (<{min_gb}GB). Free space or increase MIN_FREE_GB.",
        )


def prune_stale_workdirs(*, output_root: Path, max_age_hours: int = 24) -> int:
    """
    Remove stale work directories under Output/*/work/* older than max_age_hours.
    """
    out = Path(output_root).resolve()
    cutoff = time.time() - float(max(1, int(max_age_hours))) * 3600.0
    removed = 0
    for base in out.glob("*"):
        work_parent = base / "work"
        if not work_parent.exists() or not work_parent.is_dir():
            continue
        for wd in work_parent.glob("*"):
            try:
                if not wd.is_dir():
                    continue
                # use dir mtime as heuristic
                if wd.stat().st_mtime >= cutoff:
                    continue
                shutil.rmtree(wd, ignore_errors=True)
                removed += 1
            except Exception:
                continue
    if removed:
        logger.info("workdir_prune_done", removed=removed)
    return removed


def periodic_prune_tick(*, output_root: Path) -> int:
    s = get_settings()
    return prune_stale_workdirs(output_root=output_root, max_age_hours=int(s.work_stale_max_hours))


def _safe_under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except Exception:
        return False


def _dir_size_bytes(path: Path, *, seen: set[tuple[int, int]] | None = None) -> int:
    total = 0
    seen = seen if seen is not None else set()
    if path.is_file():
        try:
            st = path.stat()
            key = (int(st.st_dev), int(st.st_ino))
            if key in seen:
                return 0
            seen.add(key)
            return max(0, int(st.st_size))
        except Exception:
            return 0
    for root, _dirs, files in os.walk(str(path)):
        for name in files:
            p = Path(root) / name
            if p.is_symlink():
                continue
            try:
                st = p.stat()
                key = (int(st.st_dev), int(st.st_ino))
                if key in seen:
                    continue
                seen.add(key)
                total += max(0, int(st.st_size))
            except Exception:
                continue
    return total


def job_storage_bytes(*, job: Job, output_root: Path | None = None) -> int:
    out_root = Path(output_root or get_settings().output_dir).resolve()
    base_dir = get_job_output_root(job).resolve()
    library_dir = get_library_root_for_job(job).resolve()
    jobs_ptr = (out_root / "jobs" / str(job.id)).resolve()
    total = 0
    seen: set[tuple[int, int]] = set()
    for p in (base_dir, library_dir, jobs_ptr):
        if not p.exists():
            continue
        if not _safe_under_root(p, out_root):
            continue
        total += _dir_size_bytes(p, seen=seen)
    return int(total)
