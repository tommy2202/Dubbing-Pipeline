from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job, JobState
from dubbing_pipeline.jobs.store import JobStore
from dubbing_pipeline.library.paths import get_job_output_root, get_library_root_for_job
from dubbing_pipeline.ops import audit
from dubbing_pipeline.utils.log import logger
from dubbing_pipeline.utils.paths import default_paths


def _utc_now() -> float:
    return time.time()


def _cutoff_days(days: int) -> float:
    return _utc_now() - float(max(0, int(days))) * 86400.0


def _cutoff_hours(hours: int) -> float:
    return _utc_now() - float(max(0, int(hours))) * 3600.0


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


def _parse_iso_ts(ts: str) -> float | None:
    s = str(ts or "").strip()
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _uploads_root(app_root: Path | None = None) -> Path:
    try:
        return default_paths().uploads_dir.resolve()
    except Exception:
        if app_root is not None:
            return (Path(app_root).resolve() / "Input" / "uploads").resolve()
        return (Path.cwd() / "Input" / "uploads").resolve()


def _safe_delete_path(path: Path, *, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
    except Exception:
        return False
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def purge_abandoned_uploads(
    *,
    store: JobStore,
    uploads_root: Path,
    ttl_hours: int,
) -> tuple[int, int]:
    """
    Remove incomplete uploads that have been idle past ttl_hours.
    Returns (removed_count, bytes_freed).
    """
    if int(ttl_hours) <= 0:
        return 0, 0
    cutoff = _cutoff_hours(ttl_hours)
    removed = 0
    bytes_freed = 0

    for rec in store.list_uploads():
        try:
            if bool(rec.get("completed")):
                continue
            last_ts = _parse_iso_ts(str(rec.get("updated_at") or rec.get("created_at") or ""))
            if last_ts is None:
                # Fall back to part file mtime if available.
                part_path = Path(str(rec.get("part_path") or "")).resolve()
                if part_path.exists():
                    last_ts = float(part_path.stat().st_mtime)
            if last_ts is None or last_ts >= cutoff:
                continue
            owner_id = str(rec.get("owner_id") or "")
            upload_id = str(rec.get("id") or "")
            part_path = Path(str(rec.get("part_path") or "")).resolve()
            final_path = Path(str(rec.get("final_path") or "")).resolve()
            for p in [part_path, final_path]:
                if p.exists():
                    with suppress(Exception):
                        bytes_freed += int(p.stat().st_size)
                    _safe_delete_path(p, root=uploads_root)
            store.delete_upload(upload_id)
            audit.emit(
                "retention.upload.delete",
                request_id=None,
                user_id=owner_id or None,
                meta={
                    "actor": "retention",
                    "upload_id": upload_id,
                    "owner_id": owner_id,
                    "ttl_hours": int(ttl_hours),
                },
            )
            removed += 1
        except Exception as ex:
            logger.warning("retention_upload_delete_failed", error=str(ex))
            continue

    # Best-effort cleanup of empty dirs.
    with suppress(Exception):
        for d in sorted([x for x in uploads_root.rglob("*") if x.is_dir()], reverse=True):
            with suppress(StopIteration):
                next(d.iterdir())
            with suppress(Exception):
                d.rmdir()
    return removed, bytes_freed


def purge_old_inputs(*, app_root: Path, days: int) -> int:
    cutoff = _cutoff_days(days)
    uploads = _uploads_root(app_root=app_root)
    removed = 0
    for p in _iter_files(uploads):
        try:
            if not p.is_file():
                continue
            if p.stat().st_mtime >= cutoff:
                continue
            _best_effort_secure_delete(p, passes=1)
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


def _job_retention_pinned(job: Job) -> bool:
    try:
        rt = dict(job.runtime or {}) if isinstance(job.runtime, dict) else {}
    except Exception:
        rt = {}
    return bool(rt.get("pinned") or rt.get("retention_pinned") or rt.get("archived"))


def _job_age_days(job: Job) -> float:
    ts = _parse_iso_ts(str(getattr(job, "updated_at", "") or ""))
    if ts is None:
        ts = _parse_iso_ts(str(getattr(job, "created_at", "") or ""))
    if ts is None:
        return 0.0
    return max(0.0, float(_utc_now() - ts)) / 86400.0


def _safe_under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except Exception:
        return False


def delete_job_artifacts(*, job: Job, output_root: Path) -> tuple[bool, int, list[str], bool]:
    """
    Best-effort removal of job artifacts under Output/.
    Returns (deleted, bytes_freed, paths_considered).
    """
    base_dir = get_job_output_root(job).resolve()
    library_dir = get_library_root_for_job(job).resolve()
    jobs_ptr = (output_root / "jobs" / str(job.id)).resolve()
    paths = [base_dir, library_dir, jobs_ptr]
    unsafe_paths = [p for p in paths if p.exists() and not _safe_under_root(p, output_root)]
    if unsafe_paths:
        return False, 0, [str(p) for p in unsafe_paths], True
    bytes_freed = 0
    for p in paths:
        if p.exists() and _safe_under_root(p, output_root):
            with suppress(Exception):
                for sub in p.rglob("*"):
                    if sub.is_file():
                        bytes_freed += int(sub.stat().st_size)
            shutil.rmtree(p, ignore_errors=True)
    still_exists = any(p.exists() and _safe_under_root(p, output_root) for p in paths)
    return (not still_exists), int(bytes_freed), [str(p) for p in paths], False


def purge_old_job_artifacts(
    *,
    store: JobStore,
    output_root: Path,
    days: int,
) -> tuple[int, int]:
    """
    Remove old job output artifacts and metadata for jobs older than N days.
    Skips running/queued/paused jobs and pinned jobs.
    Returns (jobs_removed, bytes_freed).
    """
    if int(days) <= 0:
        return 0, 0
    removed = 0
    bytes_freed = 0
    for job in store.list_all():
        try:
            if job.state in {JobState.RUNNING, JobState.QUEUED, JobState.PAUSED}:
                continue
            if _job_retention_pinned(job):
                continue
            age_days = _job_age_days(job)
            if age_days < float(days):
                continue
            deleted, bytes_freed_job, paths, unsafe = delete_job_artifacts(
                job=job, output_root=output_root
            )
            if unsafe:
                logger.warning(
                    "retention_job_skip_unsafe",
                    job_id=str(job.id),
                    paths=paths,
                )
                continue
            bytes_freed += int(bytes_freed_job)
            # Only delete the job record if artifacts were removed (or already gone).
            if deleted:
                store.delete_job(job.id)
            audit.emit(
                "retention.job.delete",
                request_id=None,
                user_id=str(getattr(job, "owner_id", "") or "") or None,
                meta={
                    "actor": "retention",
                    "job_id": str(job.id),
                    "age_days": round(age_days, 2),
                    "retention_days": int(days),
                    "pinned": False,
                    "deleted": bool(deleted),
                },
                job_id=str(job.id),
            )
            removed += 1
        except Exception as ex:
            logger.warning("retention_job_delete_failed", job_id=str(getattr(job, "id", "")), error=str(ex))
            continue
    return removed, bytes_freed


@dataclass(frozen=True, slots=True)
class RetentionResult:
    uploads_removed: int
    jobs_removed: int
    inputs_removed: int
    logs_removed: int
    bytes_freed: int


def _resolve_store_and_root(
    *,
    store: JobStore | None,
    output_root: Path | None,
) -> tuple[JobStore, Path]:
    if store is not None:
        out_root = (Path(output_root) if output_root else Path(get_settings().output_dir)).resolve()
        return store, out_root
    s = get_settings()
    out_root = Path(s.output_dir).resolve()
    state_root = Path(getattr(s, "state_dir", None) or (out_root / "_state")).resolve()
    jobs_db = state_root / str(getattr(s, "jobs_db_name", "jobs.db") or "jobs.db")
    state_root.mkdir(parents=True, exist_ok=True)
    return JobStore(jobs_db), out_root


def run_once(
    *,
    app_root: Path | None = None,
    store: JobStore | None = None,
    output_root: Path | None = None,
) -> RetentionResult:
    s = get_settings()
    if not bool(getattr(s, "retention_enabled", False)):
        return RetentionResult(
            uploads_removed=0, jobs_removed=0, inputs_removed=0, logs_removed=0, bytes_freed=0
        )
    root = (Path(app_root) if app_root else Path(s.app_root)).resolve()
    store2, out_root = _resolve_store_and_root(store=store, output_root=output_root)
    uploads_root = _uploads_root(app_root=root)

    upload_ttl_hours = int(getattr(s, "retention_upload_ttl_hours", 0) or 0)
    if upload_ttl_hours <= 0:
        upload_ttl_hours = int(getattr(s, "retention_days_input", 0) or 0) * 24
    job_days = int(getattr(s, "retention_job_artifact_days", 0) or 0)
    log_days = int(getattr(s, "retention_log_days", 0) or 0)
    if log_days <= 0:
        log_days = int(getattr(s, "retention_days_logs", 0) or 0)

    uploads_removed, bytes_freed_uploads = purge_abandoned_uploads(
        store=store2, uploads_root=uploads_root, ttl_hours=upload_ttl_hours
    )
    jobs_removed, bytes_freed_jobs = purge_old_job_artifacts(
        store=store2, output_root=out_root, days=job_days
    )
    inputs_removed = 0
    # Legacy cleanup path for older Input/uploads structures (best-effort).
    if upload_ttl_hours >= 24:
        inputs_removed = purge_old_inputs(
            app_root=root, days=max(1, int(upload_ttl_hours // 24))
        )
    logs_removed = purge_old_logs(app_root=root, days=log_days)
    bytes_freed = int(bytes_freed_uploads + bytes_freed_jobs)
    logger.info(
        "retention_done",
        uploads_removed=uploads_removed,
        jobs_removed=jobs_removed,
        inputs_removed=inputs_removed,
        logs_removed=logs_removed,
        bytes_freed=bytes_freed,
    )
    if logs_removed > 0:
        audit.emit(
            "retention.logs",
            request_id=None,
            user_id=None,
            meta={"actor": "retention", "removed": int(logs_removed), "days": int(log_days)},
        )
    return RetentionResult(
        uploads_removed=uploads_removed,
        jobs_removed=jobs_removed,
        inputs_removed=inputs_removed,
        logs_removed=logs_removed,
        bytes_freed=bytes_freed,
    )


async def retention_loop(
    *,
    store: JobStore,
    output_root: Path,
    app_root: Path,
    interval_s: float,
) -> None:
    try:
        while True:
            try:
                await asyncio.to_thread(
                    run_once, store=store, output_root=output_root, app_root=app_root
                )
            except Exception as ex:
                logger.warning("retention_loop_failed", error=str(ex))
            await asyncio.sleep(float(interval_s))
    except asyncio.CancelledError:
        logger.info("task stopped", task="retention")
        return


def main() -> None:
    run_once()


if __name__ == "__main__":  # pragma: no cover
    main()
