from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anime_v2.utils.io import read_json, write_json
from anime_v2.utils.log import logger

_SHA256_MAX_BYTES = 256 * 1024 * 1024  # avoid hashing very large files by default


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def file_fingerprint(path: Path) -> dict[str, Any]:
    """
    Deterministic-ish fingerprint for resume checks.

    - Always includes size + mtime.
    - Includes sha256 for files up to `_SHA256_MAX_BYTES`.
    """
    path = Path(path)
    st = path.stat()
    fp: dict[str, Any] = {
        "path": str(path.resolve()),
        "size_bytes": int(st.st_size),
        "mtime": float(st.st_mtime),
        "sha256": None,
        "sha256_skipped": False,
    }
    if st.st_size <= _SHA256_MAX_BYTES:
        fp["sha256"] = _sha256_file(path)
    else:
        fp["sha256_skipped"] = True
    return fp


def params_hash(params: dict[str, Any]) -> str:
    blob = json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True, slots=True)
class StageManifest:
    stage: str
    created_at: float
    inputs: dict[str, Any]
    params: dict[str, Any]
    inputs_hash: str
    params_hash: str
    outputs: dict[str, Any]
    completed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "created_at": self.created_at,
            "completed": bool(self.completed),
            "inputs": self.inputs,
            "params": self.params,
            "inputs_hash": self.inputs_hash,
            "params_hash": self.params_hash,
            "outputs": self.outputs,
        }


def _hash_inputs(inputs: dict[str, Any]) -> str:
    blob = json.dumps(inputs, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(blob).hexdigest()


def manifest_path(job_dir: Path, stage: str) -> Path:
    return Path(job_dir) / "manifests" / f"{stage}.json"


def write_stage_manifest(
    *,
    job_dir: Path,
    stage: str,
    inputs: dict[str, Any],
    params: dict[str, Any],
    outputs: dict[str, Any],
    completed: bool = True,
) -> Path:
    p = manifest_path(job_dir, stage)
    p.parent.mkdir(parents=True, exist_ok=True)
    man = StageManifest(
        stage=str(stage),
        created_at=time.time(),
        inputs=inputs,
        params=params,
        inputs_hash=_hash_inputs(inputs),
        params_hash=params_hash(params),
        outputs=outputs,
        completed=bool(completed),
    )
    write_json(p, man.to_dict(), indent=2)
    logger.info("stage_manifest_written", stage=stage, path=str(p))
    return p


def can_resume_stage(
    *,
    job_dir: Path,
    stage: str,
    inputs: dict[str, Any],
    params: dict[str, Any],
    expected_outputs: list[Path],
) -> bool:
    """
    Resume rule:
      - manifest exists and completed
      - inputs_hash and params_hash match
      - all expected outputs exist
    """
    p = manifest_path(job_dir, stage)
    data = read_json(p, default=None)
    if not isinstance(data, dict):
        return False
    if not bool(data.get("completed")):
        return False
    if str(data.get("inputs_hash") or "") != _hash_inputs(inputs):
        return False
    if str(data.get("params_hash") or "") != params_hash(params):
        return False
    return all(Path(out).exists() for out in expected_outputs)
