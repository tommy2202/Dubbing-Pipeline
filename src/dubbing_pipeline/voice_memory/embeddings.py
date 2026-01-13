"""
Tier-2A: Offline voice embedding + matching helpers.

This module intentionally supports multiple optional dependency stacks:
- Prefer existing project embedding providers when available.
- Fall back to lightweight deterministic fingerprints when not.
"""

from __future__ import annotations

import math
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dubbing_pipeline.utils.log import logger


def _cosine_sim(a: list[float], b: list[float]) -> float:
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


def _read_wav_mono16k_f32(path: Path) -> tuple[list[float], int]:
    """
    Read PCM wav and return mono float samples in [-1,1] with the original sample rate.
    If wav is not PCM16/mono, this still attempts a best-effort read.
    """
    p = Path(path)
    with wave.open(str(p), "rb") as wf:
        ch = int(wf.getnchannels())
        sw = int(wf.getsampwidth())
        sr = int(wf.getframerate())
        n = int(wf.getnframes())
        raw = wf.readframes(n)

    # Only robustly handle PCM16; otherwise treat as silence.
    if sw != 2 or not raw:
        return ([], sr or 16000)

    # Convert little-endian int16 to float.
    samples: list[float] = []
    step = 2 * max(1, ch)
    for i in range(0, len(raw) - 1, step):
        # take first channel only
        s16 = int.from_bytes(raw[i : i + 2], byteorder="little", signed=True)
        samples.append(max(-1.0, min(1.0, float(s16) / 32768.0)))
    return samples, sr or 16000


def _fingerprint_stats(samples: list[float], *, sr: int) -> list[float]:
    """
    Very lightweight deterministic fingerprint, used only when better embeddings aren't available.
    """
    if not samples:
        return [0.0] * 16

    n = len(samples)
    rms = math.sqrt(sum(x * x for x in samples) / float(n))
    zc = 0
    prev = samples[0]
    for x in samples[1:]:
        if (prev >= 0 and x < 0) or (prev < 0 and x >= 0):
            zc += 1
        prev = x
    zcr = float(zc) / float(max(1, n - 1))
    # crude frequency estimate (works well enough for synthetic tones)
    freq_est_hz = zcr * float(max(1, int(sr))) / 2.0
    # derivative statistics (higher for higher-frequency content)
    d1 = 0.0
    d2 = 0.0
    prev = samples[0]
    prev2 = samples[0]
    for x in samples[1:]:
        dx = abs(x - prev)
        d1 += dx
        ddx = abs((x - prev) - (prev - prev2))
        d2 += ddx
        prev2 = prev
        prev = x
    d1 = d1 / float(max(1, n - 1))
    d2 = d2 / float(max(1, n - 1))

    # Goertzel magnitudes for a few probe frequencies (very discriminative for tones,
    # and still useful for real speech as a crude spectral fingerprint).
    def goertzel_mag(x: list[float], *, sr: int, f_hz: float) -> float:
        n0 = len(x)
        if n0 <= 0 or sr <= 0:
            return 0.0
        # normalized frequency bin
        k = int(0.5 + (n0 * float(f_hz) / float(sr)))
        w = (2.0 * math.pi / float(n0)) * float(k)
        cosw = math.cos(w)
        coeff = 2.0 * cosw
        s_prev = 0.0
        s_prev2 = 0.0
        for xn in x:
            s = float(xn) + coeff * s_prev - s_prev2
            s_prev2 = s_prev
            s_prev = s
        power = s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2
        return max(0.0, float(power))

    # Use at most ~0.6s for speed + consistency
    max_n = int(min(len(samples), max(1, int(sr)) * 6 // 10))
    x = samples[:max_n]
    mags = [
        goertzel_mag(x, sr=sr, f_hz=220.0),
        goertzel_mag(x, sr=sr, f_hz=440.0),
        goertzel_mag(x, sr=sr, f_hz=660.0),
        goertzel_mag(x, sr=sr, f_hz=880.0),
    ]
    # log-compress
    mags = [math.log1p(m) for m in mags]

    return [
        float(rms),
        float(zcr),
        float(freq_est_hz / 1000.0),
        float(d1),
        float(d2),
        float(mags[0]),
        float(mags[1]),
        float(mags[2]),
        float(mags[3]),
    ]


def compute_embedding(wav_path: Path, *, device: str = "cpu") -> tuple[list[float] | None, str]:
    """
    Compute an embedding vector for a speaker reference clip.

    Returns (embedding, provider_name).
    """
    p = Path(wav_path)
    if not p.exists():
        return None, "missing"

    # 1) Prefer repo's existing embedding provider (ECAPA) if available.
    try:
        from dubbing_pipeline.utils.embeds import ecapa_embedding

        emb = ecapa_embedding(p, device=device)  # numpy array or None
        if emb is not None:
            try:
                # convert to python list
                return [float(x) for x in emb.reshape(-1).tolist()], "ecapa"
            except Exception:
                return None, "ecapa"
    except Exception:
        pass

    # 2) Try resemblyzer if installed
    try:
        import numpy as np  # type: ignore
        from resemblyzer import VoiceEncoder, preprocess_wav  # type: ignore

        encoder = VoiceEncoder()
        w = preprocess_wav(str(p))
        emb2 = encoder.embed_utterance(w)
        return [
            float(x) for x in np.asarray(emb2, dtype=np.float32).reshape(-1).tolist()
        ], "resemblyzer"
    except Exception:
        pass

    # 3) Try librosa MFCC if available
    try:
        import librosa  # type: ignore
        import numpy as np  # type: ignore

        y, sr = librosa.load(str(p), sr=16000, mono=True)
        if y is None or len(y) == 0:
            return None, "librosa_mfcc"
        mfcc = librosa.feature.mfcc(y=y, sr=16000, n_mfcc=13)
        v = np.concatenate([mfcc.mean(axis=1), mfcc.std(axis=1)], axis=0).astype(np.float32)
        return [float(x) for x in v.reshape(-1).tolist()], "librosa_mfcc"
    except Exception:
        pass

    # 4) Try python_speech_features if available
    try:
        import numpy as np  # type: ignore
        import python_speech_features as psf  # type: ignore

        samples, sr = _read_wav_mono16k_f32(p)
        if not samples:
            return None, "python_speech_features"
        x = np.asarray(samples, dtype=np.float32)
        mfcc = psf.mfcc(x, samplerate=sr, numcep=13)
        v = np.concatenate([mfcc.mean(axis=0), mfcc.std(axis=0)], axis=0).astype(np.float32)
        return [float(x) for x in v.reshape(-1).tolist()], "python_speech_features"
    except Exception:
        pass

    # 5) Last-resort fingerprint (no heavy deps)
    try:
        samples, sr = _read_wav_mono16k_f32(p)
        return _fingerprint_stats(samples, sr=sr), "fingerprint"
    except Exception as ex:
        logger.warning("voice_memory_embedding_failed", error=str(ex))
        return None, "error"


@dataclass(frozen=True, slots=True)
class MatchResult:
    character_id: str
    similarity: float
    provider: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "character_id": self.character_id,
            "similarity": float(self.similarity),
            "provider": str(self.provider),
        }


def match_embedding(
    embedding: list[float],
    candidates: dict[str, list[float]],
    *,
    threshold: float,
) -> tuple[str | None, float]:
    """
    Return (best_character_id, similarity) if above threshold.
    """
    best_id = None
    best_sim = -1.0
    for cid, emb in candidates.items():
        sim = _cosine_sim(embedding, emb)
        if sim > best_sim:
            best_sim = sim
            best_id = cid
    if best_id is None or best_sim < float(threshold):
        return None, float(best_sim)
    return str(best_id), float(best_sim)
