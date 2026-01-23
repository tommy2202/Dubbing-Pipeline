from __future__ import annotations

import hashlib
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


def _sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _sha256_embedding(embedding: list[float]) -> str:
    try:
        payload = json.dumps(
            [round(float(x), 6) for x in embedding], separators=(",", ":"), sort_keys=False
        ).encode("utf-8")
    except Exception:
        return ""
    return _sha256_bytes(payload)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


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


def _character_meta_path(series_root: Path, character_slug: str) -> Path:
    return (_character_root(series_root, character_slug) / "meta.json").resolve()


def _versions_path(series_root: Path, character_slug: str) -> Path:
    return (_character_root(series_root, character_slug) / "versions.json").resolve()


def _load_versions(series_root: Path, character_slug: str) -> dict[str, Any]:
    path = _versions_path(series_root, character_slug)
    data = read_json(path, default={})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("series_slug", _slugify(series_root.name))
    data.setdefault("character_slug", _slugify(character_slug))
    if not isinstance(data.get("versions"), list):
        data["versions"] = []
    return data


def _save_versions(series_root: Path, character_slug: str, data: dict[str, Any]) -> None:
    path = _versions_path(series_root, character_slug)
    _dump_json(path, data)


def list_versions(
    series_slug: str, character_slug: str, *, voice_store_dir: Path | None = None
) -> list[dict[str, Any]]:
    sr = get_series_root(series_slug, voice_store_dir=voice_store_dir)
    data = _load_versions(sr, character_slug)
    items = data.get("versions")
    if not isinstance(items, list):
        return []
    out = [dict(x) for x in items if isinstance(x, dict)]
    out.sort(key=lambda x: int(x.get("version") or 0))
    return out


def get_versions_state(
    series_slug: str, character_slug: str, *, voice_store_dir: Path | None = None
) -> dict[str, Any]:
    sr = get_series_root(series_slug, voice_store_dir=voice_store_dir)
    data = _load_versions(sr, character_slug)
    items = data.get("versions") if isinstance(data.get("versions"), list) else []
    out = [dict(x) for x in items if isinstance(x, dict)]
    out.sort(key=lambda x: int(x.get("version") or 0))
    current = int(data.get("current_version") or 0)
    if not current and out:
        current = int(out[-1].get("version") or 0)
    return {"current_version": current, "items": out}


def _record_version(
    *,
    series_slug: str,
    character_slug: str,
    history_ref: Path,
    source_ref: Path,
    job_id: str,
    metadata: dict[str, Any] | None,
    voice_store_dir: Path | None = None,
) -> dict[str, Any]:
    sr = get_series_root(series_slug, voice_store_dir=voice_store_dir)
    data = _load_versions(sr, character_slug)
    items = data.get("versions") if isinstance(data.get("versions"), list) else []
    items = [dict(x) for x in items if isinstance(x, dict)]
    prev = items[-1] if items else None
    next_version = int(prev.get("version") or 0) + 1 if prev else 1
    ts = _now_ts()
    notes = str((metadata or {}).get("notes") or "").strip()
    created_by = str((metadata or {}).get("created_by") or "").strip()
    source = str((metadata or {}).get("source") or "").strip()
    drift_threshold = float(get_settings().voice_drift_threshold)

    emb_hash = ""
    emb_provider = ""
    similarity = None
    drifted = None
    drift_provider = ""
    try:
        from dubbing_pipeline.voice_memory.embeddings import match_embedding
        from dubbing_pipeline.voice_store.embeddings import get_or_compute_embedding

        emb_new, provider_new = get_or_compute_embedding(
            Path(history_ref),
            allow_fingerprint=False,
        )
        emb_provider = str(provider_new or "")
        if emb_new is not None:
            emb_hash = _sha256_embedding(emb_new)
            prev_ref = None
            if isinstance(prev, dict):
                prev_ref = prev.get("ref_path")
            if prev_ref:
                emb_prev, provider_prev = get_or_compute_embedding(
                    Path(prev_ref),
                    allow_fingerprint=False,
                )
                drift_provider = str(provider_prev or "")
                if emb_prev is not None:
                    _, similarity = match_embedding(
                        emb_new, {"prev": emb_prev}, threshold=-1.0
                    )
                    drifted = bool(float(similarity) < drift_threshold)
    except Exception:
        emb_hash = ""
        emb_provider = ""
        similarity = None
        drifted = None

    entry = {
        "version": int(next_version),
        "created_at": int(ts),
        "ref_path": str(Path(history_ref).resolve()),
        "ref_sha256": _sha256_file(history_ref),
        "refs": [str(Path(source_ref).resolve())],
        "job_id": str(job_id or ""),
        "created_by": created_by,
        "notes": notes,
        "source": source,
        "embedding_hash": emb_hash,
        "embedding_provider": emb_provider,
        "drift_provider": drift_provider or emb_provider,
        "similarity": float(similarity) if similarity is not None else None,
        "drifted": bool(drifted) if drifted is not None else None,
        "drift_threshold": float(drift_threshold),
    }
    items.append(entry)
    data["versions"] = items
    data["current_version"] = int(next_version)
    _save_versions(sr, character_slug, data)
    return entry


def rollback_character_ref(
    series_slug: str,
    character_slug: str,
    *,
    version: int,
    created_by: str,
    voice_store_dir: Path | None = None,
) -> Path:
    sr = get_series_root(series_slug, voice_store_dir=voice_store_dir)
    items = list_versions(series_slug, character_slug, voice_store_dir=voice_store_dir)
    target = None
    for it in items:
        if int(it.get("version") or 0) == int(version):
            target = it
            break
    if target is None:
        raise FileNotFoundError(f"version {version} not found")
    ref_path = Path(str(target.get("ref_path") or "")).resolve()
    if not ref_path.exists():
        raise FileNotFoundError(str(ref_path))
    meta = read_json(_character_meta_path(sr, character_slug), default={})
    if not isinstance(meta, dict):
        meta = {}
    meta.setdefault("display_name", "")
    meta.setdefault("created_by", str(created_by or ""))
    meta["notes"] = str(meta.get("notes") or "")
    return save_character_ref(
        series_slug,
        character_slug,
        ref_path,
        job_id=f"rollback_v{int(version)}",
        metadata={
            "display_name": str(meta.get("display_name") or ""),
            "created_by": str(created_by or ""),
            "notes": f"rollback to v{int(version)}",
            "source": "rollback",
        },
        voice_store_dir=voice_store_dir,
    )


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
    prev_meta = read_json(cr / "meta.json", default={})
    if isinstance(prev_meta, dict):
        if str(prev_meta.get("display_name") or "").strip():
            md.setdefault("display_name", str(prev_meta.get("display_name") or "").strip())
        if str(prev_meta.get("notes") or "").strip():
            md.setdefault("notes", str(prev_meta.get("notes") or "").strip())
        if str(prev_meta.get("created_by") or "").strip():
            md.setdefault("created_by", str(prev_meta.get("created_by") or "").strip())
    md.setdefault("series_slug", _slugify(series_slug))
    md.setdefault("character_slug", _slugify(character_slug))
    md.setdefault("job_id", str(job_id))
    md.setdefault("created_by", str(md.get("created_by") or ""))
    md["last_updated_ts"] = int(ts)
    md["ref_path"] = str(canonical)
    _dump_json(cr / "meta.json", md)

    # versions.json (character scoped)
    with suppress(Exception):
        _record_version(
            series_slug=series_slug,
            character_slug=character_slug,
            history_ref=hist,
            source_ref=src,
            job_id=str(job_id),
            metadata=md,
            voice_store_dir=voice_store_dir,
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

