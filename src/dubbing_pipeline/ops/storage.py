from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from fastapi import HTTPException

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
