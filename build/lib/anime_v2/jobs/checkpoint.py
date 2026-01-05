from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from anime_v2.utils.log import logger


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _artifact_record(path: Path) -> dict[str, Any]:
    st = path.stat()
    return {
        "path": str(path),
        "sha256": _sha256_file(path),
        "size": int(st.st_size),
        "mtime": float(st.st_mtime),
    }


def _ckpt_path_from(meta: dict[str, Any] | None, *, ckpt_path: Path | None = None) -> Path:
    if ckpt_path is not None:
        return Path(ckpt_path)
    meta = meta or {}
    p = meta.get("ckpt_path")
    if p:
        return Path(str(p))
    wd = meta.get("work_dir")
    if wd:
        return Path(str(wd)) / ".checkpoint.json"
    raise RuntimeError("checkpoint path not provided (pass ckpt_path=... or meta['work_dir'])")


def read_ckpt(
    job_id: str, *, ckpt_path: Path | None = None, meta: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    path = _ckpt_path_from(meta, ckpt_path=ckpt_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        if str(data.get("job_id") or "") not in {"", str(job_id)}:
            # tolerate missing job_id in older formats
            logger.warning("checkpoint_job_id_mismatch", expected=job_id, found=data.get("job_id"))
        return data
    except Exception as ex:
        logger.warning("checkpoint_read_failed", path=str(path), error=str(ex))
        return None


def _artifacts_valid(artifacts: dict[str, Any]) -> bool:
    if not isinstance(artifacts, dict) or not artifacts:
        return False
    for _, rec in artifacts.items():
        try:
            p = Path(str(rec["path"]))
            if not p.exists() or not p.is_file():
                return False
            sha = str(rec.get("sha256") or "")
            if sha and _sha256_file(p) != sha:
                return False
        except Exception:
            return False
    return True


def stage_is_done(ckpt: dict[str, Any] | None, stage: str) -> bool:
    if not ckpt or not isinstance(ckpt, dict):
        return False
    stages = ckpt.get("stages", {})
    if not isinstance(stages, dict):
        return False
    entry = stages.get(stage)
    if not isinstance(entry, dict):
        return False
    if not bool(entry.get("done")):
        return False
    return _artifacts_valid(entry.get("artifacts", {}))


def write_ckpt(
    job_id: str,
    stage: str,
    artifacts: dict[str, str | Path],
    meta: dict[str, Any] | None,
    *,
    ckpt_path: Path | None = None,
) -> Path:
    """
    Atomic checkpoint write.
    Stores per-stage artifact sha256 so we can safely skip work on restart.
    """
    path = _ckpt_path_from(meta, ckpt_path=ckpt_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # read existing
    cur = read_ckpt(job_id, ckpt_path=path) or {"version": 1, "job_id": job_id, "stages": {}}
    if not isinstance(cur.get("stages"), dict):
        cur["stages"] = {}

    recs: dict[str, Any] = {}
    for k, p in (artifacts or {}).items():
        pp = Path(str(p))
        if not pp.exists():
            continue
        recs[str(k)] = _artifact_record(pp)

    entry = {"done": True, "done_at": time.time(), "artifacts": recs, "meta": (meta or {})}
    cur["job_id"] = job_id
    cur["last_stage"] = str(stage)
    cur["updated_at"] = time.time()
    cur["stages"][str(stage)] = entry

    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def advance_stage(
    job_id: str,
    next_stage: str,
    artifacts: dict[str, str | Path],
    *,
    meta: dict[str, Any] | None = None,
    ckpt_path: Path | None = None,
) -> Path:
    return write_ckpt(job_id, next_stage, artifacts, meta, ckpt_path=ckpt_path)
