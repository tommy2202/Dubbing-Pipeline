from __future__ import annotations

import json
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.io import atomic_copy, atomic_write_text, read_json


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(s: str) -> str:
    t = str(s or "").strip().lower()
    t = _SLUG_RE.sub("-", t)
    t = re.sub(r"-{2,}", "-", t).strip("-")
    return t


def _now_ts() -> int:
    return int(time.time())


def _safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _dump_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


@dataclass(frozen=True, slots=True)
class CharacterRecord:
    series_slug: str
    character_slug: str
    display_name: str
    ref_path: str
    updated_at: str
    created_by: str


def _root(voice_store_dir: Path | None = None) -> Path:
    if voice_store_dir is not None:
        return Path(voice_store_dir).resolve()
    return Path(get_settings().voice_store_dir).resolve()


def get_series_root(series_slug: str, *, voice_store_dir: Path | None = None) -> Path:
    """
    Series root under the global voice store.
    """
    slug = _slugify(series_slug)
    if not slug:
        raise ValueError("series_slug required")
    return (_root(voice_store_dir) / slug).resolve()


def _series_index_path(series_root: Path) -> Path:
    return (series_root / "index.json").resolve()


def _character_root(series_root: Path, character_slug: str) -> Path:
    cslug = _slugify(character_slug)
    if not cslug:
        raise ValueError("character_slug required")
    return (series_root / "characters" / cslug).resolve()


def list_characters(series_slug: str, *, voice_store_dir: Path | None = None) -> list[dict[str, Any]]:
    sr = get_series_root(series_slug, voice_store_dir=voice_store_dir)
    idx_path = _series_index_path(sr)
    data = read_json(idx_path, default={})
    items = data.get("characters") if isinstance(data, dict) else None
    if not isinstance(items, list):
        items = []
    out: list[dict[str, Any]] = []
    for it in items:
        if isinstance(it, dict):
            out.append(dict(it))
    out.sort(key=lambda x: str(x.get("character_slug") or ""))
    return out


def get_character_ref(
    series_slug: str, character_slug: str, *, voice_store_dir: Path | None = None
) -> Path | None:
    sr = get_series_root(series_slug, voice_store_dir=voice_store_dir)
    cr = _character_root(sr, character_slug)
    ref = cr / "ref.wav"
    return ref if ref.exists() and ref.is_file() else None


def save_character_ref(
    series_slug: str,
    character_slug: str,
    ref_wav: Path,
    job_id: str,
    metadata: dict[str, Any] | None,
    *,
    voice_store_dir: Path | None = None,
) -> Path:
    """
    Save a new reference WAV for a series character.

    - Writes canonical `ref.wav` (copy)
    - Writes history copy under `refs/<job_id>_<ts>.wav`
    - Updates `meta.json` and series `index.json`
    """
    src = Path(ref_wav).resolve()
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(str(src))
    sr = get_series_root(series_slug, voice_store_dir=voice_store_dir)
    cr = _character_root(sr, character_slug)
    _safe_mkdir(cr)
    _safe_mkdir(cr / "refs")
    _safe_mkdir(sr / "characters")

    ts = _now_ts()
    safe_job = _slugify(job_id) or "job"
    hist = (cr / "refs" / f"{safe_job}_{ts}.wav").resolve()
    canonical = (cr / "ref.wav").resolve()

    atomic_copy(src, hist)
    atomic_copy(src, canonical)

    # meta.json (character scoped)
    md = dict(metadata or {})
    md.setdefault("series_slug", _slugify(series_slug))
    md.setdefault("character_slug", _slugify(character_slug))
    md.setdefault("job_id", str(job_id))
    md.setdefault("created_by", str(md.get("created_by") or ""))
    md["last_updated_ts"] = int(ts)
    md["ref_path"] = str(canonical)
    _dump_json(cr / "meta.json", md)

    # series index.json
    idx_path = _series_index_path(sr)
    idx = read_json(idx_path, default={})
    if not isinstance(idx, dict):
        idx = {}
    idx.setdefault("version", 1)
    idx.setdefault("series_slug", _slugify(series_slug))
    chars = idx.get("characters")
    if not isinstance(chars, list):
        chars = []
        idx["characters"] = chars
    cslug = _slugify(character_slug)
    display_name = str(md.get("display_name") or md.get("name") or "").strip()
    created_by = str(md.get("created_by") or "").strip()

    # upsert by character_slug
    found = None
    for it in chars:
        if not isinstance(it, dict):
            continue
        if str(it.get("character_slug") or "") == cslug:
            found = it
            break
    rec = found if found is not None else {}
    rec.update(
        {
            "character_slug": cslug,
            "display_name": display_name,
            "ref_path": str(canonical),
            "updated_at": str(md.get("updated_at") or md.get("last_updated_ts") or ts),
            "created_by": created_by,
        }
    )
    if found is None:
        chars.append(rec)
    # keep stable ordering
    chars.sort(key=lambda x: str(x.get("character_slug") or ""))
    _dump_json(idx_path, idx)
    return canonical


def delete_character(
    series_slug: str, character_slug: str, *, voice_store_dir: Path | None = None
) -> bool:
    """
    Delete a character folder and remove it from the series index.
    Returns True if something was deleted.
    """
    sr = get_series_root(series_slug, voice_store_dir=voice_store_dir)
    cr = _character_root(sr, character_slug)
    deleted = False
    if cr.exists():
        # best-effort recursive delete
        with suppress(Exception):
            for p in sorted(cr.rglob("*"), reverse=True):
                with suppress(Exception):
                    if p.is_file() or p.is_symlink():
                        p.unlink(missing_ok=True)
                    elif p.is_dir():
                        p.rmdir()
            with suppress(Exception):
                cr.rmdir()
            deleted = True

    # update series index.json (best-effort)
    with suppress(Exception):
        idx_path = _series_index_path(sr)
        idx = read_json(idx_path, default={})
        if isinstance(idx, dict) and isinstance(idx.get("characters"), list):
            cslug = _slugify(character_slug)
            before = len(idx["characters"])
            idx["characters"] = [
                x
                for x in idx["characters"]
                if not (isinstance(x, dict) and str(x.get("character_slug") or "") == cslug)
            ]
            if len(idx["characters"]) != before:
                _dump_json(idx_path, idx)
                deleted = True
    return bool(deleted)

