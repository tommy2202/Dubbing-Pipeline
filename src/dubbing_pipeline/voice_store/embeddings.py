"""
Optional voice embeddings for the series-scoped voice store.

This module is intentionally tolerant of missing optional dependencies.
Auto-matching should call compute_embedding(..., allow_fingerprint=False).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from dubbing_pipeline.utils.io import atomic_write_text, read_json
from dubbing_pipeline.utils.log import logger
from dubbing_pipeline.voice_memory.embeddings import (
    compute_embedding as _compute_embedding,
    match_embedding as _match_embedding,
)
from dubbing_pipeline.voice_store.store import get_character_ref, list_characters


def _embedding_path(ref_path: Path) -> Path:
    return (Path(ref_path).resolve().parent / "embedding.json").resolve()


def _is_fresh(ref_path: Path, embedding_path: Path) -> bool:
    try:
        if not embedding_path.exists():
            return False
        if not ref_path.exists():
            return False
        return float(embedding_path.stat().st_mtime) >= float(ref_path.stat().st_mtime)
    except Exception:
        return False


def _load_embedding_json(path: Path) -> dict[str, Any] | None:
    data = read_json(path, default=None)
    if not isinstance(data, dict):
        return None
    emb = data.get("embedding")
    if not isinstance(emb, list):
        return None
    return data


def load_embedding(ref_path: Path) -> tuple[list[float] | None, str]:
    """
    Load an embedding from disk if present.
    Returns (embedding, provider).
    """
    ref_path = Path(ref_path).resolve()
    meta_path = _embedding_path(ref_path)
    data = _load_embedding_json(meta_path)
    if not data:
        return None, "missing"
    if str(data.get("ref_path") or "") and str(data.get("ref_path") or "") != str(ref_path):
        return None, "stale"
    emb_raw = data.get("embedding")
    if not isinstance(emb_raw, list):
        return None, "invalid"
    try:
        emb = [float(x) for x in emb_raw]
    except Exception:
        return None, "invalid"
    return emb, str(data.get("provider") or "unknown")


def save_embedding(ref_path: Path, embedding: list[float], *, provider: str) -> None:
    ref_path = Path(ref_path).resolve()
    meta_path = _embedding_path(ref_path)
    payload = {
        "version": 1,
        "ref_path": str(ref_path),
        "provider": str(provider or ""),
        "updated_at": float(time.time()),
        "embedding": [float(x) for x in embedding],
    }
    atomic_write_text(meta_path, json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def compute_embedding(
    wav_path: Path, *, device: str = "cpu", allow_fingerprint: bool = False
) -> tuple[list[float] | None, str]:
    """
    Compute an embedding for a WAV path.

    Returns (embedding, provider). When allow_fingerprint=False, this returns None
    if only the deterministic fingerprint fallback is available.
    """
    emb, provider = _compute_embedding(Path(wav_path), device=device)
    if emb is None:
        return None, provider
    if provider == "fingerprint" and not allow_fingerprint:
        return None, "fingerprint_unavailable"
    return emb, provider


def get_or_compute_embedding(
    ref_path: Path,
    *,
    device: str = "cpu",
    allow_fingerprint: bool = False,
    refresh: bool = False,
) -> tuple[list[float] | None, str]:
    """
    Load a cached embedding when fresh, else compute + persist.
    """
    ref_path = Path(ref_path).resolve()
    meta_path = _embedding_path(ref_path)
    if not refresh and _is_fresh(ref_path, meta_path):
        emb, provider = load_embedding(ref_path)
        if emb:
            return emb, provider
    emb, provider = compute_embedding(
        ref_path, device=device, allow_fingerprint=allow_fingerprint
    )
    if emb is None:
        return None, provider
    try:
        save_embedding(ref_path, emb, provider=provider)
    except Exception as ex:
        logger.warning("voice_store_embedding_cache_failed", error=str(ex))
    return emb, provider


def load_series_character_embeddings(
    series_slug: str,
    *,
    voice_store_dir: Path | None = None,
    device: str = "cpu",
    allow_fingerprint: bool = False,
    refresh: bool = False,
) -> tuple[dict[str, list[float]], dict[str, str]]:
    """
    Load or compute embeddings for all characters with refs in a series.

    Returns (embeddings, providers) keyed by character_slug.
    """
    embeddings: dict[str, list[float]] = {}
    providers: dict[str, str] = {}
    for it in list_characters(series_slug, voice_store_dir=voice_store_dir):
        if not isinstance(it, dict):
            continue
        cslug = str(it.get("character_slug") or "").strip()
        if not cslug:
            continue
        ref = get_character_ref(series_slug, cslug, voice_store_dir=voice_store_dir)
        if ref is None or not ref.exists():
            continue
        emb, provider = get_or_compute_embedding(
            ref,
            device=device,
            allow_fingerprint=allow_fingerprint,
            refresh=refresh,
        )
        if emb is None:
            continue
        embeddings[cslug] = emb
        providers[cslug] = provider
    return embeddings, providers


def match_embedding(
    embedding: list[float],
    candidates: dict[str, list[float]],
    *,
    threshold: float,
) -> tuple[str | None, float]:
    return _match_embedding(embedding, candidates, threshold=float(threshold))


def suggest_matches(
    *,
    series_slug: str,
    speaker_refs: dict[str, Path],
    threshold: float,
    device: str = "cpu",
    voice_store_dir: Path | None = None,
    allow_fingerprint: bool = False,
) -> list[dict[str, Any]]:
    """
    Suggest speaker->character matches using voice embeddings similarity.
    """
    candidates, _ = load_series_character_embeddings(
        series_slug,
        voice_store_dir=voice_store_dir,
        device=device,
        allow_fingerprint=allow_fingerprint,
    )
    if not candidates:
        return []
    out: list[dict[str, Any]] = []
    for sid, ref_path in speaker_refs.items():
        safe_sid = Path(str(sid or "")).name.strip()
        if not safe_sid:
            continue
        emb, provider = compute_embedding(
            ref_path, device=device, allow_fingerprint=allow_fingerprint
        )
        if emb is None:
            continue
        best_id, best_sim = match_embedding(emb, candidates, threshold=float(threshold))
        if best_id is None:
            continue
        out.append(
            {
                "speaker_id": safe_sid,
                "character_slug": str(best_id),
                "similarity": float(best_sim),
                "provider": str(provider),
            }
        )
    return out
