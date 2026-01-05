from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anime_v2.security.crypto import (
    decrypt_bytes,
    encryption_enabled_for,
    is_encrypted_path,
    write_bytes_encrypted,
)
from anime_v2.utils.io import atomic_write_text, read_json, write_json
from anime_v2.utils.log import logger


def overrides_path(job_dir: Path) -> Path:
    return Path(job_dir) / "review" / "overrides.json"


def overrides_applied_log_path(job_dir: Path) -> Path:
    return Path(job_dir) / "analysis" / "overrides_applied.jsonl"


def _now_utc() -> str:
    return __import__("datetime").datetime.now(tz=__import__("datetime").UTC).isoformat()


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_json(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return _sha256_bytes(blob)


def default_overrides() -> dict[str, Any]:
    return {
        "version": 1,
        "music_regions_overrides": {"adds": [], "removes": [], "edits": []},
        "speaker_overrides": {},  # segment_id -> forced_character_id
        "smoothing_overrides": {"disable_segments": [], "disable_ranges": []},
    }


def load_overrides(job_dir: Path) -> dict[str, Any]:
    p = overrides_path(job_dir)
    if encryption_enabled_for("review") and is_encrypted_path(p):
        try:
            blob = p.read_bytes()
            pt = decrypt_bytes(blob, kind="review", job_id=str(Path(job_dir).name))
            data = json.loads(pt.decode("utf-8"))
        except Exception:
            data = None
    else:
        data = read_json(p, default=None)
    if not isinstance(data, dict):
        return default_overrides()
    out = default_overrides()
    # merge known keys only
    for k in ["version", "music_regions_overrides", "speaker_overrides", "smoothing_overrides"]:
        if k in data:
            out[k] = data.get(k)
    if not isinstance(out.get("music_regions_overrides"), dict):
        out["music_regions_overrides"] = {"adds": [], "removes": [], "edits": []}
    if not isinstance(out.get("speaker_overrides"), dict):
        out["speaker_overrides"] = {}
    if not isinstance(out.get("smoothing_overrides"), dict):
        out["smoothing_overrides"] = {"disable_segments": [], "disable_ranges": []}
    out["version"] = 1
    return out


def save_overrides(job_dir: Path, overrides: dict[str, Any]) -> Path:
    job_dir = Path(job_dir)
    p = overrides_path(job_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = default_overrides()
    if isinstance(overrides, dict):
        payload.update({k: overrides.get(k) for k in payload if k in overrides})
    payload["version"] = 1
    raw = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    if encryption_enabled_for("review"):
        write_bytes_encrypted(p, raw, kind="review", job_id=str(Path(job_dir).name))
    else:
        atomic_write_text(p, raw.decode("utf-8"), encoding="utf-8")
    return p


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)


def _overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
    return max(0.0, min(a1, b1) - max(a0, b0)) > 0.0


def _match_region(
    regs: list[dict[str, Any]], start: float, end: float, *, eps: float = 0.05
) -> int | None:
    best = None
    best_d = 1e9
    for i, r in enumerate(regs):
        try:
            s = float(r.get("start", 0.0))
            e = float(r.get("end", 0.0))
        except Exception:
            continue
        d = abs(s - float(start)) + abs(e - float(end))
        if d <= eps * 2 and d < best_d:
            best = i
            best_d = d
    return best


def apply_music_region_overrides(
    base_regions: list[dict[str, Any]], overrides: dict[str, Any]
) -> list[dict[str, Any]]:
    """
    Apply {adds, removes, edits} to a base region list (dict regions).
    Deterministic order:
      - edits (by match)
      - removes (by match)
      - adds (append)
      - final sort + merge (best-effort via music_detect._merge_regions style semantics not required here)
    """
    regs = [
        dict(r)
        for r in (base_regions if isinstance(base_regions, list) else [])
        if isinstance(r, dict)
    ]
    mro = overrides.get("music_regions_overrides") if isinstance(overrides, dict) else None
    if not isinstance(mro, dict):
        return regs
    edits = mro.get("edits") if isinstance(mro.get("edits"), list) else []
    removes = mro.get("removes") if isinstance(mro.get("removes"), list) else []
    adds = mro.get("adds") if isinstance(mro.get("adds"), list) else []

    # edits
    for it in edits:
        if not isinstance(it, dict):
            continue
        fr = it.get("from")
        to = it.get("to")
        if not isinstance(fr, dict) or not isinstance(to, dict):
            continue
        try:
            i = _match_region(regs, float(fr.get("start")), float(fr.get("end")))
        except Exception:
            i = None
        if i is None:
            continue
        # update allowed fields
        for k in ["start", "end", "kind", "confidence", "reason"]:
            if k in to:
                regs[i][k] = to.get(k)
        regs[i]["source"] = "user_edit"

    # removes
    for it in removes:
        if not isinstance(it, dict):
            continue
        try:
            i = _match_region(regs, float(it.get("start")), float(it.get("end")))
        except Exception:
            i = None
        if i is None:
            continue
        regs[i]["_remove"] = True
        regs[i]["_remove_reason"] = str(it.get("reason") or "user_remove")
    regs = [r for r in regs if not r.get("_remove")]

    # adds
    for it in adds:
        if not isinstance(it, dict):
            continue
        try:
            s = float(it.get("start"))
            e = float(it.get("end"))
        except Exception:
            continue
        if e <= s:
            continue
        regs.append(
            {
                "start": s,
                "end": e,
                "kind": str(it.get("kind") or "music"),
                "confidence": float(it.get("confidence") or 1.0),
                "reason": str(it.get("reason") or "user_add"),
                "source": "user_add",
            }
        )

    # normalize + sort
    out = []
    for r in regs:
        try:
            s = float(r.get("start", 0.0))
            e = float(r.get("end", 0.0))
            if e <= s:
                continue
            out.append(
                {
                    "start": max(0.0, s),
                    "end": max(0.0, e),
                    "kind": str(r.get("kind") or "music"),
                    "confidence": float(r.get("confidence") or 0.0),
                    "reason": str(r.get("reason") or ""),
                    "source": str(r.get("source") or r.get("reason") or "base"),
                }
            )
        except Exception:
            continue
    out.sort(key=lambda x: (float(x["start"]), float(x["end"]), str(x.get("kind") or "")))
    return out


def effective_music_regions_for_job(job_dir: Path) -> tuple[list[dict[str, Any]], Path]:
    """
    Reads base regions from Output/<job>/analysis/music_regions.json if present.
    Applies overrides from Output/<job>/review/overrides.json.
    Writes Output/<job>/analysis/music_regions.effective.json.
    """
    job_dir = Path(job_dir)
    base_path = job_dir / "analysis" / "music_regions.json"
    base = []
    if base_path.exists():
        data = read_json(base_path, default={})
        regs = data.get("regions", []) if isinstance(data, dict) else []
        if isinstance(regs, list):
            base = [r for r in regs if isinstance(r, dict)]

    ov = load_overrides(job_dir)
    eff = apply_music_region_overrides(base, ov)

    out_path = job_dir / "analysis" / "music_regions.effective.json"
    write_json(out_path, {"version": 1, "base_path": str(base_path), "regions": eff})
    return eff, out_path


def apply_speaker_overrides_to_segments(
    segments: list[dict[str, Any]], speaker_overrides: dict[str, Any]
) -> tuple[list[dict[str, Any]], int]:
    """
    Apply segment_id -> forced_character_id to a segments list.
    Supports keys:
      - segment_id (preferred) OR index field variants.
      - speaker / speaker_id fields updated to forced_character_id.
    """
    if not isinstance(speaker_overrides, dict) or not segments:
        return segments, 0
    out = []
    changed = 0
    for i, s in enumerate(segments, 1):
        if not isinstance(s, dict):
            continue
        sid = int(s.get("segment_id") or s.get("index") or i)
        forced = speaker_overrides.get(str(sid))
        if forced is None:
            forced = speaker_overrides.get(int(sid))  # type: ignore[arg-type]
        if forced is not None and str(forced).strip():
            s2 = dict(s)
            s2["speaker"] = str(forced).strip()
            s2["speaker_id"] = str(forced).strip()
            s2["character_id"] = str(forced).strip()
            s2["_speaker_overridden"] = True
            out.append(s2)
            changed += 1
        else:
            out.append(s)
    return out, changed


def apply_smoothing_overrides_to_utts(
    utts: list[dict[str, Any]],
    smoothing_overrides: dict[str, Any],
    *,
    segment_ranges: list[tuple[float, float]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """
    Revert speaker smoothing changes for selected time ranges / segments.
    Expects smoothing to have stored `speaker_original` on modified utts.
    """
    if not isinstance(smoothing_overrides, dict) or not utts:
        return utts, 0
    disable_segments = smoothing_overrides.get("disable_segments", [])
    disable_ranges = smoothing_overrides.get("disable_ranges", [])

    ranges: list[tuple[float, float]] = []
    if isinstance(disable_ranges, list):
        for r in disable_ranges:
            if not isinstance(r, dict):
                continue
            try:
                s = float(r.get("start"))
                e = float(r.get("end"))
            except Exception:
                continue
            if e > s:
                ranges.append((s, e))
    if segment_ranges is not None and isinstance(disable_segments, list):
        for sid in disable_segments:
            try:
                idx = int(sid) - 1
            except Exception:
                continue
            if 0 <= idx < len(segment_ranges):
                s, e = segment_ranges[idx]
                if e > s:
                    ranges.append((float(s), float(e)))

    if not ranges:
        return utts, 0

    ranges.sort()
    out = []
    reverted = 0
    for u in utts:
        if not isinstance(u, dict):
            continue
        s = float(u.get("start", 0.0))
        e = float(u.get("end", s))
        if any(_overlaps(s, e, a0, a1) for a0, a1 in ranges) and (
            "speaker_original" in u and str(u.get("speaker_original") or "").strip()
        ):
            u2 = dict(u)
            u2["speaker"] = str(u.get("speaker_original"))
            u2["_smoothing_reverted"] = True
            out.append(u2)
            reverted += 1
            continue
        out.append(u)
    return out, reverted


@dataclass(frozen=True, slots=True)
class OverridesApplyReport:
    overrides_hash: str
    effective_music_regions_path: str
    speaker_overrides: int
    smoothing_reverted_utts: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "overrides_hash": self.overrides_hash,
            "effective_music_regions_path": self.effective_music_regions_path,
            "speaker_overrides": int(self.speaker_overrides),
            "smoothing_reverted_utts": int(self.smoothing_reverted_utts),
        }


def apply_overrides(job_dir: Path, *, write_manifest: bool = True) -> OverridesApplyReport:
    """
    Apply overrides *deterministically* by writing:
      - analysis/music_regions.effective.json (base + overrides)
      - manifests/overrides.json (hash + references)
      - analysis/overrides_applied.jsonl (events)

    NOTE: This does not automatically regenerate missing TTS clips; it only
    writes the effective artifacts that the pipeline reads on the next run.
    """
    job_dir = Path(job_dir)
    ov = load_overrides(job_dir)
    ov_hash = _sha256_json(ov)

    # Always compute effective music regions (may be empty).
    _, eff_path = effective_music_regions_for_job(job_dir)

    # Speaker overrides count (application happens in stages; we log intent here)
    sp = ov.get("speaker_overrides") if isinstance(ov, dict) else None
    sp_count = len(sp) if isinstance(sp, dict) else 0

    rep = OverridesApplyReport(
        overrides_hash=ov_hash,
        effective_music_regions_path=str(eff_path),
        speaker_overrides=sp_count,
        smoothing_reverted_utts=0,
    )

    _append_jsonl(
        overrides_applied_log_path(job_dir),
        {
            "ts": _now_utc(),
            "event": "overrides_apply",
            "job_dir": str(job_dir),
            "overrides_hash": ov_hash,
            "speaker_overrides": sp_count,
            "effective_music_regions_path": str(eff_path),
        },
    )

    if write_manifest:
        man = {
            "version": 1,
            "created_at": time.time(),
            "overrides_path": str(overrides_path(job_dir)),
            "overrides_hash": ov_hash,
            "effective_music_regions_path": str(eff_path),
        }
        (job_dir / "manifests").mkdir(parents=True, exist_ok=True)
        write_json(job_dir / "manifests" / "overrides.json", man, indent=2)
        logger.info("overrides_manifest_written", job_dir=str(job_dir), overrides_hash=ov_hash)

    return rep
