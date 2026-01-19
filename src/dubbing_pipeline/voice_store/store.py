from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.io import atomic_copy, atomic_write_text, read_json
from dubbing_pipeline.utils.single_writer import writer_lock


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(s: str) -> str:
    t = str(s or "").strip().lower()
    t = _SLUG_RE.sub("-", t)
    t = re.sub(r"-{2,}", "-", t).strip("-")
    return t


def _now_ts() -> int:
    return int(time.time())


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _dump_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _version_id() -> str:
    return str(int(time.time() * 1000))


def _version_root(series_root: Path, character_slug: str) -> Path:
    return (_character_root(series_root, character_slug) / "versions").resolve()


def _write_version(
    *,
    series_root: Path,
    character_slug: str,
    ref_wav: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    vid = _version_id()
    vroot = _version_root(series_root, character_slug)
    vdir = (vroot / vid).resolve()
    _safe_mkdir(vdir)
    ref_out = (vdir / "ref.wav").resolve()
    atomic_copy(Path(ref_wav).resolve(), ref_out)
    meta = dict(metadata or {})
    meta.setdefault("version_id", vid)
    meta.setdefault("created_at", _now_iso())
    meta.setdefault("ref_path", str(ref_out))
    _dump_json(vdir / "metadata.json", meta)
    return {"version_id": vid, "ref_path": str(ref_out), "metadata": meta}


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
    with writer_lock("voice_store.save_character_ref"):
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
        md.setdefault("source", str(md.get("source") or "unknown"))
        md["last_updated_ts"] = int(ts)
        md["ref_path"] = str(canonical)
        _dump_json(cr / "meta.json", md)

        # versions/<ts>/ref.wav + metadata.json
        with suppress(Exception):
            _write_version(
                series_root=sr,
                character_slug=character_slug,
                ref_wav=canonical,
                metadata={
                    "series_slug": _slugify(series_slug),
                    "character_slug": _slugify(character_slug),
                    "display_name": str(md.get("display_name") or md.get("name") or ""),
                    "job_id": str(job_id),
                    "created_by": str(md.get("created_by") or ""),
                    "source": str(md.get("source") or "unknown"),
                    "updated_at": str(md.get("updated_at") or md.get("last_updated_ts") or ts),
                },
            )

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
    with writer_lock("voice_store.delete_character"):
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


def list_character_versions(
    series_slug: str, character_slug: str, *, voice_store_dir: Path | None = None
) -> list[dict[str, Any]]:
    sr = get_series_root(series_slug, voice_store_dir=voice_store_dir)
    cslug = _slugify(character_slug)
    vroot = _version_root(sr, cslug)
    if not vroot.exists():
        return []
    items: list[dict[str, Any]] = []
    for vdir in sorted(vroot.iterdir(), reverse=True):
        if not vdir.is_dir():
            continue
        vid = vdir.name
        ref_path = vdir / "ref.wav"
        meta_path = vdir / "metadata.json"
        meta = read_json(meta_path, default={})
        if not isinstance(meta, dict):
            meta = {}
        meta.setdefault("version_id", vid)
        meta.setdefault("series_slug", _slugify(series_slug))
        meta.setdefault("character_slug", cslug)
        meta.setdefault("ref_path", str(ref_path) if ref_path.exists() else "")
        meta.setdefault("created_at", "")
        items.append(
            {
                "version_id": str(meta.get("version_id") or vid),
                "ref_path": str(meta.get("ref_path") or ""),
                "created_at": str(meta.get("created_at") or ""),
                "job_id": str(meta.get("job_id") or ""),
                "created_by": str(meta.get("created_by") or ""),
                "source": str(meta.get("source") or ""),
                "display_name": str(meta.get("display_name") or ""),
            }
        )
    return items


def get_character_version(
    series_slug: str,
    character_slug: str,
    version_id: str,
    *,
    voice_store_dir: Path | None = None,
) -> dict[str, Any] | None:
    sr = get_series_root(series_slug, voice_store_dir=voice_store_dir)
    cslug = _slugify(character_slug)
    vid = str(version_id or "").strip()
    if not vid:
        return None
    vdir = (_version_root(sr, cslug) / vid).resolve()
    ref_path = vdir / "ref.wav"
    if not vdir.exists() or not ref_path.exists():
        return None
    meta_path = vdir / "metadata.json"
    meta = read_json(meta_path, default={})
    if not isinstance(meta, dict):
        meta = {}
    meta.setdefault("version_id", vid)
    meta.setdefault("series_slug", _slugify(series_slug))
    meta.setdefault("character_slug", cslug)
    meta.setdefault("ref_path", str(ref_path))
    return meta


def rollback_character_ref(
    *,
    series_slug: str,
    character_slug: str,
    version_id: str,
    created_by: str = "",
    voice_store_dir: Path | None = None,
) -> Path:
    meta = get_character_version(
        series_slug, character_slug, version_id, voice_store_dir=voice_store_dir
    )
    if not meta:
        raise FileNotFoundError("version not found")
    ref_path = Path(str(meta.get("ref_path") or "")).resolve()
    if not ref_path.exists():
        raise FileNotFoundError("version ref missing")
    return save_character_ref(
        series_slug,
        character_slug,
        ref_path,
        job_id=str(meta.get("job_id") or "rollback"),
        metadata={
            "display_name": str(meta.get("display_name") or ""),
            "created_by": str(created_by or ""),
            "source": "rollback",
            "rollback_version": str(version_id),
        },
        voice_store_dir=voice_store_dir,
    )

