from __future__ import annotations

import argparse
from pathlib import Path

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.store import JobStore
from dubbing_pipeline.ops.storage import job_storage_bytes


def _safe_under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except Exception:
        return False


def _input_uploads_dir(*, app_root: Path) -> Path:
    s = get_settings()
    if getattr(s, "input_uploads_dir", None):
        return Path(str(s.input_uploads_dir)).resolve()
    input_dir = Path(str(getattr(s, "input_dir", "") or (app_root / "Input"))).resolve()
    return (input_dir / "uploads").resolve()


def _resolve_store(*, app_root: Path | None = None, state_dir: Path | None = None) -> JobStore:
    s = get_settings()
    root = (Path(app_root) if app_root else Path(s.app_root)).resolve()
    state_root = Path(
        state_dir
        if state_dir is not None
        else (getattr(s, "state_dir", None) or (Path(s.output_dir) / "_state"))
    ).resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    jobs_db = state_root / str(getattr(s, "jobs_db_name", "jobs.db") or "jobs.db")
    return JobStore(jobs_db)


def main() -> int:
    parser = argparse.ArgumentParser(description="Recompute per-user storage accounting.")
    parser.add_argument("--app-root", default=None, help="Override APP_ROOT path")
    parser.add_argument("--state-dir", default=None, help="Override state dir (jobs.db)")
    parser.add_argument("--output-dir", default=None, help="Override output dir")
    parser.add_argument("--dry-run", action="store_true", help="Compute totals without writing")
    args = parser.parse_args()

    app_root = Path(args.app_root).resolve() if args.app_root else None
    output_root = (
        Path(args.output_dir).resolve() if args.output_dir else Path(get_settings().output_dir).resolve()
    )
    store = _resolve_store(app_root=app_root, state_dir=Path(args.state_dir).resolve() if args.state_dir else None)
    uploads_root = _input_uploads_dir(app_root=app_root or Path(get_settings().app_root).resolve())

    job_entries: list[tuple[str, str, int]] = []
    for job in store.list_all():
        try:
            uid = str(getattr(job, "owner_id", "") or "").strip()
            if not uid:
                continue
            size = job_storage_bytes(job=job, output_root=output_root)
            job_entries.append((str(job.id), uid, int(size)))
        except Exception:
            continue

    upload_entries: list[tuple[str, str, int]] = []
    for rec in store.list_uploads():
        try:
            if not bool(rec.get("completed")):
                continue
            uid = str(rec.get("owner_id") or "").strip()
            upid = str(rec.get("id") or "").strip()
            if not uid or not upid:
                continue
            final_path = Path(str(rec.get("final_path") or "")).resolve()
            if not final_path.exists() or not _safe_under_root(final_path, uploads_root):
                size = 0
            else:
                size = int(final_path.stat().st_size)
            upload_entries.append((upid, uid, int(size)))
        except Exception:
            continue

    totals: dict[str, int] = {}
    for _job_id, uid, size in job_entries:
        totals[uid] = int(totals.get(uid, 0)) + int(size)
    for _upload_id, uid, size in upload_entries:
        totals[uid] = int(totals.get(uid, 0)) + int(size)

    print(f"jobs={len(job_entries)} uploads={len(upload_entries)} users={len(totals)}")
    for uid, size in sorted(totals.items(), key=lambda it: it[1], reverse=True):
        print(f"{uid}\t{int(size)}")

    if args.dry_run:
        return 0

    store.replace_storage_accounting(job_entries=job_entries, upload_entries=upload_entries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
