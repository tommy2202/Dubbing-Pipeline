from __future__ import annotations

import math
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from anime_v2.utils.ffmpeg_safe import extract_audio_mono_16k


@dataclass(frozen=True, slots=True)
class ProsodyFeatures:
    start_s: float
    end_s: float
    duration_s: float
    rms: float | None
    pitch_hz: float | None
    cps: float | None  # characters-per-second (text proxy)
    wps: float | None  # words-per-second (text proxy)
    category: str  # calm|neutral|excited|angry|sad
    signals: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _rms_pcm16(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                return None
            frames = wf.readframes(wf.getnframes())
        if not frames:
            return 0.0
        # int16 little-endian
        n = len(frames) // 2
        if n <= 0:
            return 0.0
        s2 = 0.0
        for i in range(0, n * 2, 2):
            v = int.from_bytes(frames[i : i + 2], "little", signed=True)
            s2 += float(v * v)
        return math.sqrt(s2 / float(n)) / 32768.0
    except Exception:
        return None


def _pitch_librosa(path: Path, *, sr: int = 16000) -> float | None:
    """
    Optional pitch proxy using librosa if installed.
    Returns median f0 in Hz, or None.
    """
    try:
        import librosa  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        return None
    try:
        y, _sr = librosa.load(str(path), sr=sr, mono=True)
        if y is None or len(y) < sr // 10:
            return None
        f0 = librosa.yin(y, fmin=60, fmax=400, sr=sr)
        f0 = np.asarray(f0, dtype=float)
        f0 = f0[np.isfinite(f0)]
        if f0.size == 0:
            return None
        return float(np.median(f0))
    except Exception:
        return None


def _text_proxies(text: str, duration_s: float) -> tuple[float | None, float | None]:
    t = " ".join(str(text or "").split()).strip()
    if not t or duration_s <= 0:
        return None, None
    cps = float(len(t)) / float(duration_s)
    words = [w for w in t.replace("…", " ").replace("...", " ").split() if w.strip()]
    wps = float(len(words)) / float(duration_s) if words else 0.0
    return cps, wps


def categorize(*, rms: float | None, pitch_hz: float | None, text: str) -> tuple[str, dict[str, Any]]:
    """
    Coarse offline category heuristic.
    """
    t = str(text or "")
    sig: dict[str, Any] = {"bang": "!" in t, "q": "?" in t, "ell": ("..." in t or "…" in t)}

    # Default neutral
    cat = "neutral"
    r = float(rms) if rms is not None else None
    p = float(pitch_hz) if pitch_hz is not None else None

    if sig["ell"] and (r is None or r < 0.06):
        cat = "sad"
    if sig["bang"] and (r is None or r >= 0.06):
        cat = "excited"
    # Very loud + bang → angry-ish
    if sig["bang"] and r is not None and r >= 0.12:
        cat = "angry"
    # Very low energy → calm
    if r is not None and r < 0.04 and not sig["bang"]:
        cat = "calm"
    # Pitch proxy can nudge excited/calm
    if p is not None and p > 230 and cat in {"neutral", "excited"}:
        cat = "excited"
    if p is not None and p < 140 and cat in {"neutral"} and r is not None and r < 0.06:
        cat = "calm"

    sig["rms"] = r
    sig["pitch_hz"] = p
    sig["text_len"] = len(t)
    return cat, sig


def analyze_segment(
    *,
    source_audio_wav: Path,
    start_s: float,
    end_s: float,
    text: str,
    out_wav: Path,
    pitch: bool = True,
) -> ProsodyFeatures:
    """
    Extracts a segment WAV (mono16k) and computes lightweight prosody features.
    """
    source_audio_wav = Path(source_audio_wav)
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.0, float(end_s) - float(start_s))

    if dur <= 0.0:
        return ProsodyFeatures(
            start_s=float(start_s),
            end_s=float(end_s),
            duration_s=float(dur),
            rms=None,
            pitch_hz=None,
            cps=None,
            wps=None,
            category="neutral",
            signals={"error": "zero_duration"},
        )

    extract_audio_mono_16k(
        src=source_audio_wav,
        dst=out_wav,
        start_s=float(start_s),
        end_s=float(end_s),
        timeout_s=120,
    )
    rms = _rms_pcm16(out_wav)
    pitch_hz = _pitch_librosa(out_wav) if pitch else None
    cps, wps = _text_proxies(text, dur)
    category, signals = categorize(rms=rms, pitch_hz=pitch_hz, text=text)
    return ProsodyFeatures(
        start_s=float(start_s),
        end_s=float(end_s),
        duration_s=float(dur),
        rms=rms,
        pitch_hz=pitch_hz,
        cps=cps,
        wps=wps,
        category=str(category),
        signals=signals,
    )

