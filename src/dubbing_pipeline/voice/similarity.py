from __future__ import annotations

import math
from pathlib import Path

from dubbing_pipeline.utils.log import logger
from dubbing_pipeline.voice_memory.embeddings import compute_embedding


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=False):
        fx = float(x)
        fy = float(y)
        dot += fx * fy
        na += fx * fx
        nb += fy * fy
    denom = math.sqrt(na) * math.sqrt(nb)
    if denom <= 0:
        return -1.0
    return float(dot / denom)


def compute_embedding_or_mfcc(wav_path: Path) -> tuple[list[float] | None, str, str]:
    """
    Compute an embedding (or MFCC/fingerprint fallback) and return
    (embedding, provider, disclaimer).
    """
    emb, provider = compute_embedding(Path(wav_path), device="cpu")
    if emb is None:
        return None, str(provider or "missing"), "embedding unavailable"
    disclaimer = ""
    if provider in {"librosa_mfcc", "python_speech_features"}:
        disclaimer = "MFCC fallback (heuristic similarity)"
    elif provider == "fingerprint":
        disclaimer = "Fingerprint fallback (coarse similarity)"
    return emb, str(provider or "unknown"), disclaimer


def similarity(a: list[float] | None, b: list[float] | None) -> float | None:
    if not a or not b:
        return None
    return _cosine(a, b)


def compare_refs(
    *,
    current_ref: Path,
    new_ref: Path,
) -> dict[str, object]:
    """
    Compare two reference WAVs and return similarity info.
    """
    out: dict[str, object] = {
        "similarity": None,
        "provider": "unknown",
        "disclaimer": "",
    }
    try:
        emb_a, prov_a, disc_a = compute_embedding_or_mfcc(current_ref)
        emb_b, prov_b, disc_b = compute_embedding_or_mfcc(new_ref)
        sim = similarity(emb_a, emb_b)
        provider = prov_a if prov_a == prov_b else f"{prov_a}|{prov_b}"
        disclaimer = disc_a or disc_b
        out.update(
            {
                "similarity": sim,
                "provider": provider,
                "disclaimer": disclaimer,
            }
        )
        return out
    except Exception as ex:
        logger.warning("voice_similarity_failed", error=str(ex))
        return out
