from __future__ import annotations

import hashlib
import json
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anime_v2.config import get_settings
from anime_v2.utils.log import logger


def _utc_ts() -> str:
    # YYYYmmdd-HHMM
    return time.strftime("%Y%m%d-%H%M", time.gmtime())


def _sha256_path(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel(root: Path, p: Path) -> str:
    pr = p.resolve()
    rr = root.resolve()
    try:
        return str(pr.relative_to(rr)).replace("\\", "/")
    except Exception:
        # If output/log dirs are mounted outside APP_ROOT, keep a stable zip path anyway.
        return ("external/" + str(pr).lstrip("/")).replace("\\", "/")


def _iter_backup_files(app_root: Path) -> Iterable[Path]:
    """
    Back up manifests/metadata only (no large media):
    - data/** (incl. encrypted characters.json)
    - Output/*.db
    - Output/**/{*.json,*.srt,job.log}
    """
    data_dir = (app_root / "data").resolve()
    if data_dir.exists():
        yield from [p for p in data_dir.rglob("*") if p.is_file()]

    out_dir = Path(get_settings().output_dir).resolve()
    if out_dir.exists():
        for p in out_dir.glob("*.db"):
            if p.is_file():
                yield p
        for p in out_dir.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() in {".json", ".srt"} or p.name == "job.log":
                yield p


@dataclass(frozen=True, slots=True)
class BackupResult:
    zip_path: Path
    manifest_path: Path
    file_count: int


def create_backup(*, app_root: Path | None = None) -> BackupResult:
    root = (Path(app_root) if app_root else Path(get_settings().app_root)).resolve()
    backups_dir = (root / "backups").resolve()
    backups_dir.mkdir(parents=True, exist_ok=True)

    stamp = _utc_ts()
    zip_path = backups_dir / f"backup-{stamp}.zip"
    manifest_path = backups_dir / f"backup-{stamp}.manifest.json"

    files = sorted({p.resolve() for p in _iter_backup_files(root)})
    manifest: dict[str, Any] = {
        "version": 1,
        "created_at": stamp,
        "root": str(root),
        "files": [],
    }

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in files:
            try:
                rel = _rel(root, p)
            except Exception:
                continue
            try:
                sha = _sha256_path(p)
                size = p.stat().st_size
            except Exception:
                continue
            z.write(p, arcname=rel)
            manifest["files"].append({"path": rel, "sha256": sha, "size": int(size)})

        # Embed manifest inside zip as well
        z.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))

    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    logger.info(
        "backup_created",
        zip=str(zip_path),
        manifest=str(manifest_path),
        files=len(manifest["files"]),
    )

    s3 = get_settings().backup_s3_url
    if s3:
        # Optional upload: best-effort via awscli if available.
        try:
            import shutil
            import subprocess

            if shutil.which("aws"):
                subprocess.run(
                    ["aws", "s3", "cp", str(zip_path), s3.rstrip("/") + "/" + zip_path.name],
                    check=True,
                )
                subprocess.run(
                    [
                        "aws",
                        "s3",
                        "cp",
                        str(manifest_path),
                        s3.rstrip("/") + "/" + manifest_path.name,
                    ],
                    check=True,
                )
                logger.info("backup_uploaded", dest=s3)
            else:
                logger.warning("backup_upload_skipped", reason="aws_cli_not_found", dest=s3)
        except Exception as ex:
            logger.warning("backup_upload_failed", dest=s3, error=str(ex))

    return BackupResult(
        zip_path=zip_path, manifest_path=manifest_path, file_count=len(manifest["files"])
    )


def main() -> None:
    create_backup()


if __name__ == "__main__":  # pragma: no cover
    main()
