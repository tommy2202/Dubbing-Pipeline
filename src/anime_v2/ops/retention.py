from __future__ import annotations

import os
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from anime_v2.config import get_settings
from anime_v2.utils.log import logger


def _utc_now() -> float:
    return time.time()


def _cutoff_days(days: int) -> float:
    return _utc_now() - float(max(0, int(days))) * 86400.0


def _best_effort_secure_delete(path: Path, *, passes: int = 1) -> None:
    """
    Best-effort secure delete:
    - overwrite with zeros
    - fsync
    - unlink

    Note: not guaranteed on journaling / CoW filesystems / SSDs.
    """
    try:
        if not path.exists() or not path.is_file():
            return
        size = path.stat().st_size
        if size <= 0:
            path.unlink(missing_ok=True)
            return
        with path.open("r+b") as f:
            for _ in range(max(1, passes)):
                f.seek(0)
                remaining = size
                chunk = b"\x00" * (1024 * 1024)
                while remaining > 0:
                    n = min(len(chunk), remaining)
                    f.write(chunk[:n])
                    remaining -= n
                f.flush()
                os.fsync(f.fileno())
        path.unlink(missing_ok=True)
    except Exception as ex:
        logger.warning("secure_delete_failed", path=str(path), error=str(ex))
        with suppress(Exception):
            path.unlink(missing_ok=True)


def _iter_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    yield from root.rglob("*")


def purge_old_inputs(*, app_root: Path, days: int) -> int:
    cutoff = _cutoff_days(days)
    # Prefer configured uploads dir (web/API); fall back to historical APP_ROOT/Input/uploads.
    s = get_settings()
    uploads: Path
    if getattr(s, "input_uploads_dir", None):
        uploads = Path(str(s.input_uploads_dir)).resolve()
    elif getattr(s, "input_dir", None):
        uploads = (Path(str(s.input_dir)).resolve() / "uploads").resolve()
    else:
        uploads = (app_root / "Input" / "uploads").resolve()
    removed = 0
    for p in _iter_files(uploads):
        try:
            if not p.is_file():
                continue
            if p.stat().st_mtime >= cutoff:
                continue
            _best_effort_secure_delete(p)
            removed += 1
        except Exception:
            continue
    # Clean empty dirs
    try:
        for d in sorted([x for x in uploads.rglob("*") if x.is_dir()], reverse=True):
            try:
                next(d.iterdir())
            except StopIteration:
                d.rmdir()
    except Exception:
        pass
    return removed


def purge_old_logs(*, app_root: Path, days: int) -> int:
    cutoff = _cutoff_days(days)
    removed = 0

    s = get_settings()
    # global logs/
    logs_dir = Path(s.log_dir).resolve()
    for p in _iter_files(logs_dir):
        try:
            if not p.is_file():
                continue
            if p.stat().st_mtime >= cutoff:
                continue
            # logs are not sensitive; normal delete is fine, but we'll best-effort wipe anyway.
            _best_effort_secure_delete(p, passes=1)
            removed += 1
        except Exception:
            continue

    # per-job logs: Output/**/job.log
    out_dir = Path(s.output_dir).resolve()
    for p in out_dir.glob("**/job.log"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                _best_effort_secure_delete(p, passes=1)
                removed += 1
        except Exception:
            continue
    return removed


@dataclass(frozen=True, slots=True)
class RetentionResult:
    inputs_removed: int
    logs_removed: int


def run_once(*, app_root: Path | None = None) -> RetentionResult:
    s = get_settings()
    root = (Path(app_root) if app_root else Path(s.app_root)).resolve()
    inputs_removed = purge_old_inputs(app_root=root, days=s.retention_days_input)
    logs_removed = purge_old_logs(app_root=root, days=s.retention_days_logs)
    logger.info("retention_done", inputs_removed=inputs_removed, logs_removed=logs_removed)
    return RetentionResult(inputs_removed=inputs_removed, logs_removed=logs_removed)


def main() -> None:
    run_once()


if __name__ == "__main__":  # pragma: no cover
    main()
