from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

from anime_v2.utils.io import read_json, write_json
from anime_v2.utils.log import logger

RetentionPolicy = Literal["full", "balanced", "minimal"]


@dataclass(frozen=True, slots=True)
class DeleteAction:
    kind: str  # file|dir
    path: Path
    bytes: int
    reason: str


def _safe_stat_bytes(p: Path) -> int:
    try:
        if p.is_file():
            return int(p.stat().st_size)
    except Exception:
        return 0
    return 0


def _iter_paths(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return root.rglob("*")


def _job_age_days(job_dir: Path) -> float:
    try:
        st = job_dir.stat()
        age_s = max(0.0, time.time() - float(st.st_mtime))
        return age_s / 86400.0
    except Exception:
        return 0.0


def _collect_references(job_dir: Path) -> set[Path]:
    """
    Best-effort: collect any file paths referenced by manifests/review/tts manifests.
    We will avoid deleting referenced files even if they look like "heavy".
    """
    refs: set[Path] = set()

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)
        elif isinstance(obj, str):
            s = obj.strip()
            if not s:
                return
            try:
                p = Path(s)
                # only consider paths under job_dir
                if p.is_absolute():
                    try:
                        p2 = p.resolve()
                    except Exception:
                        p2 = p
                    if str(p2).startswith(str(job_dir.resolve())):
                        refs.add(p2)
                else:
                    p2 = (job_dir / p).resolve()
                    if str(p2).startswith(str(job_dir.resolve())):
                        refs.add(p2)
            except Exception:
                return

    candidates = [
        job_dir / "manifests",
        job_dir / "review" / "state.json",
        job_dir / "tts_manifest.json",
    ]
    for c in candidates:
        try:
            if c.is_dir():
                for p in c.glob("**/*.json"):
                    data = read_json(p, default=None)
                    _walk(data)
            elif c.is_file() and c.suffix.lower() == ".json":
                data = read_json(c, default=None)
                _walk(data)
        except Exception:
            continue
    return refs


def _atomic_delete_file(p: Path) -> None:
    tmp = p.with_name(p.name + f".trash.{os.getpid()}")
    try:
        p.replace(tmp)
    except Exception:
        tmp = p
    try:
        tmp.unlink(missing_ok=True)
    except Exception:
        # last resort: leave renamed file
        return


def _atomic_delete_dir(p: Path) -> None:
    tmp = p.with_name(p.name + f".trashdir.{os.getpid()}")
    try:
        p.replace(tmp)
        shutil.rmtree(tmp, ignore_errors=True)
        return
    except Exception:
        shutil.rmtree(p, ignore_errors=True)


def _final_outputs(job_dir: Path) -> list[Path]:
    """Never delete by default."""
    out = []
    for pat in ("dub.mkv", "dub.mp4", "*.dub.mkv", "*.dub.mp4", "final_lipsynced.mp4", "*.final_lipsynced.mp4"):
        out.extend(list(job_dir.glob(pat)))
    # streaming stitched final
    out.extend(list((job_dir / "stream").glob("final*.mp4")))
    # mobile artifacts (mobile_update): keep by default
    out.extend(list((job_dir / "mobile").glob("mobile.mp4")))
    out.extend(list((job_dir / "mobile" / "hls").glob("index.m3u8")))
    out.extend(list((job_dir / "mobile" / "hls").glob("*.ts")))
    return [p for p in out if p.exists()]


def _important_intermediates(job_dir: Path) -> list[Path]:
    out = []
    out.extend(list(job_dir.glob("*.srt")))
    out.extend(list(job_dir.glob("*.vtt")))
    out.extend(list((job_dir / "subs").glob("*.srt")))
    out.extend(list((job_dir / "subs").glob("*.vtt")))
    out.extend([job_dir / "translated.json", job_dir / "diarization.json"])
    out.extend(list((job_dir / "qa").glob("*")))
    out.extend(list((job_dir / "analysis").glob("*.json")))
    out.extend(list((job_dir / "analysis").glob("*.jsonl")))
    out.extend(list((job_dir / "expressive").glob("**/*")))
    out.extend(list((job_dir / "manifests").glob("*.json")))
    out.extend(list((job_dir / "review").glob("state.json")))
    # per-job human log (kept even in minimal mode)
    out.extend([job_dir / "job.log"])
    return [p for p in out if p.exists()]


def _heavy_intermediates(job_dir: Path) -> list[Path]:
    out = []
    out.extend(list((job_dir / "stems").glob("**/*")))
    out.extend(list((job_dir / "chunks").glob("**/*")))
    out.extend(list((job_dir / "stream").glob("chunk_*.mp4")))
    out.extend(list((job_dir / "stream").glob("chunk_*/*.wav")))
    out.extend(list((job_dir / "stream").glob("chunk_*/*.json")))
    out.extend(list((job_dir / "segments").glob("**/*")))
    out.extend(list((job_dir / "audio" / "tracks").glob("**/*")))
    out.extend(list((job_dir / "tmp").glob("**/*")))
    return [p for p in out if p.exists()]


def _temp_files(job_dir: Path) -> list[Path]:
    out = []
    # common temp markers
    for p in _iter_paths(job_dir):
        try:
            if p.is_file() and ".tmp" in p.name:
                out.append(p)
        except Exception:
            continue
    return out


def apply_retention(
    job_dir: str | Path,
    policy: str,
    *,
    retention_days: int = 0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Apply per-job retention policy to an Output/<job> directory.

    Policies:
      - full: keep everything (no deletions)
      - balanced: remove heavy intermediates + temp
      - minimal: keep only final outputs + essential logs/manifests and small analysis; remove heavy intermediates

    Safety:
      - final outputs are never deleted by default
      - referenced paths (manifests/review) are preserved
      - report is always written to Output/<job>/analysis/retention_report.json
    """
    job_dir = Path(job_dir).resolve()
    pol = str(policy or "full").strip().lower()
    if pol not in {"full", "balanced", "minimal"}:
        pol = "full"

    age_days = _job_age_days(job_dir)
    if int(retention_days) > 0 and age_days < float(retention_days):
        report = {
            "job_dir": str(job_dir),
            "policy": pol,
            "dry_run": bool(dry_run),
            "retention_days": int(retention_days),
            "job_age_days": round(age_days, 3),
            "skipped": True,
            "skip_reason": "job_too_new_for_retention_days",
            "deleted": [],
            "kept_final_outputs": [str(p) for p in _final_outputs(job_dir)],
            "bytes_freed": 0,
        }
        (job_dir / "analysis").mkdir(parents=True, exist_ok=True)
        write_json(job_dir / "analysis" / "retention_report.json", report)
        return report

    finals = {p.resolve() for p in _final_outputs(job_dir)}
    refs = _collect_references(job_dir)

    deletions: list[DeleteAction] = []

    if pol == "full":
        # keep everything
        pass
    elif pol == "balanced":
        for p in _heavy_intermediates(job_dir) + _temp_files(job_dir):
            try:
                pr = p.resolve()
            except Exception:
                pr = p
            if pr in finals or pr in refs:
                continue
            if p.name == "retention_report.json":
                continue
            if p.is_dir():
                deletions.append(DeleteAction(kind="dir", path=p, bytes=0, reason="balanced:heavy_or_tmp"))
            elif p.is_file():
                deletions.append(DeleteAction(kind="file", path=p, bytes=_safe_stat_bytes(p), reason="balanced:heavy_or_tmp"))
    else:  # minimal
        keep = set()
        keep |= finals
        # essential logs and manifests
        for p in (job_dir / "logs").glob("**/*"):
            with __import__("contextlib").suppress(Exception):
                keep.add(p.resolve())
        for p in (job_dir / "manifests").glob("**/*.json"):
            with __import__("contextlib").suppress(Exception):
                keep.add(p.resolve())
        # keep key subtitles and core json artifacts
        for p in _important_intermediates(job_dir):
            with __import__("contextlib").suppress(Exception):
                keep.add(p.resolve())

        for p in _iter_paths(job_dir):
            try:
                pr = p.resolve()
            except Exception:
                pr = p
            if pr in keep or pr in finals or pr in refs:
                continue
            if p.is_dir():
                # never delete the job root itself
                continue
            if p.is_file():
                deletions.append(DeleteAction(kind="file", path=p, bytes=_safe_stat_bytes(p), reason="minimal:prune"))

        # prune known heavy dirs as dirs
        for d in [job_dir / "stems", job_dir / "chunks", job_dir / "segments", job_dir / "tmp", job_dir / "audio" / "tracks"]:
            if d.exists() and d.is_dir():
                try:
                    if d.resolve() not in keep:
                        deletions.append(DeleteAction(kind="dir", path=d, bytes=0, reason="minimal:prune_dir"))
                except Exception:
                    deletions.append(DeleteAction(kind="dir", path=d, bytes=0, reason="minimal:prune_dir"))

    # Execute deletions
    bytes_freed = 0
    deleted: list[dict[str, Any]] = []
    if deletions:
        logger.info("retention_start", job_dir=str(job_dir), policy=pol, dry_run=bool(dry_run), n=len(deletions))
    for act in deletions:
        p = Path(act.path)
        if not p.exists():
            continue
        if dry_run:
            deleted.append({"kind": act.kind, "path": str(p), "bytes": int(act.bytes), "reason": act.reason, "dry_run": True})
            continue
        try:
            if act.kind == "dir" and p.is_dir():
                _atomic_delete_dir(p)
                deleted.append({"kind": "dir", "path": str(p), "bytes": 0, "reason": act.reason})
            elif act.kind == "file" and p.is_file():
                bytes_freed += int(act.bytes)
                _atomic_delete_file(p)
                deleted.append({"kind": "file", "path": str(p), "bytes": int(act.bytes), "reason": act.reason})
        except Exception as ex:
            deleted.append({"kind": act.kind, "path": str(p), "bytes": int(act.bytes), "reason": act.reason, "error": str(ex)})

    report = {
        "job_dir": str(job_dir),
        "policy": pol,
        "dry_run": bool(dry_run),
        "retention_days": int(retention_days),
        "job_age_days": round(age_days, 3),
        "skipped": False,
        "deleted": deleted,
        "kept_final_outputs": [str(p) for p in sorted(finals, key=lambda x: str(x))],
        "referenced_paths_kept": len(refs),
        "bytes_freed": int(bytes_freed),
    }
    (job_dir / "analysis").mkdir(parents=True, exist_ok=True)
    write_json(job_dir / "analysis" / "retention_report.json", report)
    return report

