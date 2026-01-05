"""
Offline segment pacing utilities.

Uses ffmpeg for time-stretching and pad/trim. Falls back to Python wave duration
measurement when ffprobe is unavailable.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anime_v2.config import get_settings
from anime_v2.utils.ffmpeg_safe import ffprobe_duration_seconds, run_ffmpeg


def measure_wav_seconds(path: Path) -> float:
    """
    Measure duration with ffprobe when possible; fall back to wave.
    """
    p = Path(path)
    try:
        return float(ffprobe_duration_seconds(p, timeout_s=10))
    except Exception:
        pass
    try:
        with wave.open(str(p), "rb") as wf:
            n = wf.getnframes()
            sr = wf.getframerate()
        return float(n) / float(sr) if sr else 0.0
    except Exception:
        return 0.0


def compute_ratio(actual: float, target: float) -> float:
    """
    Ratio > 1 means the audio is longer than target and must be sped up.
    """
    a = max(0.0, float(actual))
    t = max(0.0001, float(target))
    return a / t


def _atempo_chain(tempo: float) -> str:
    """
    ffmpeg atempo supports 0.5..2.0; chain if needed.
    """
    r = max(0.25, min(4.0, float(tempo)))
    parts: list[str] = []
    while r > 2.0:
        parts.append("atempo=2.0")
        r /= 2.0
    while r < 0.5:
        parts.append("atempo=0.5")
        r /= 0.5
    parts.append(f"atempo={r:.4f}")
    return ",".join(parts)


def atempo_chain(tempo: float) -> str:
    """
    Public wrapper for building a safe `atempo` filter chain.

    Kept as a single source of truth (used by both pacing and prosody controls).
    """
    return _atempo_chain(tempo)


def time_stretch_wav(
    in_wav: Path,
    out_wav: Path,
    ratio: float,
    *,
    min_ratio: float = 0.88,
    max_ratio: float = 1.18,
    timeout_s: int = 120,
) -> Path:
    """
    Time-stretch using ffmpeg atempo.

    `ratio` is actual/target:
      - ratio > 1.0 speeds up (shorter)
      - ratio < 1.0 slows down (longer)
    """
    in_wav = Path(in_wav)
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)

    r = max(float(min_ratio), min(float(max_ratio), float(ratio)))
    filt = _atempo_chain(r)
    s = get_settings()
    cmd = [
        str(s.ffmpeg_bin),
        "-y",
        "-i",
        str(in_wav),
        "-filter:a",
        filt,
        "-ac",
        "1",
        "-ar",
        "16000",
        str(out_wav),
    ]
    run_ffmpeg(cmd, timeout_s=timeout_s, retries=0, capture=True)
    return out_wav


def pad_or_trim_wav(
    in_wav: Path, out_wav: Path, target_seconds: float, *, timeout_s: int = 120
) -> Path:
    """
    Force audio to a target duration by padding silence or trimming.
    """
    in_wav = Path(in_wav)
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)

    tgt = max(0.0, float(target_seconds))
    s = get_settings()
    cmd = [
        str(s.ffmpeg_bin),
        "-y",
        "-i",
        str(in_wav),
        "-af",
        f"apad,atrim=0:{tgt:.3f},asetpts=N/SR/TB",
        "-ac",
        "1",
        "-ar",
        "16000",
        str(out_wav),
    ]
    run_ffmpeg(cmd, timeout_s=timeout_s, retries=0, capture=True)
    return out_wav


@dataclass(frozen=True, slots=True)
class PacingReport:
    target_s: float
    actual_s: float
    ratio: float
    action: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_s": self.target_s,
            "actual_s": self.actual_s,
            "ratio": self.ratio,
            "action": self.action,
            "note": self.note,
        }


def match_segment_duration(
    tts_wav: Path,
    target_seconds: float,
    *,
    tolerance: float = 0.10,
    min_ratio: float = 0.88,
    max_ratio: float = 1.18,
) -> tuple[Path, PacingReport]:
    """
    Match a single segment WAV to a target duration.

    This function only performs audio-domain actions (stretch/pad/trim).
    Re-synthesis is orchestrated at a higher layer (TTS stage).
    """
    tts_wav = Path(tts_wav)
    tgt = max(0.0, float(target_seconds))
    actual = measure_wav_seconds(tts_wav)
    ratio = compute_ratio(actual, tgt) if tgt > 0 else 1.0

    # within tolerance => keep
    if tgt <= 0.0:
        return tts_wav, PacingReport(target_s=tgt, actual_s=actual, ratio=ratio, action="noop")

    if abs(actual - tgt) <= tgt * float(tolerance):
        return tts_wav, PacingReport(target_s=tgt, actual_s=actual, ratio=ratio, action="noop")

    if actual > tgt:
        # Try atempo stretch within bounds
        out = tts_wav.with_suffix(".pacing.stretch.wav")
        try:
            out2 = time_stretch_wav(
                tts_wav,
                out,
                ratio,
                min_ratio=float(min_ratio),
                max_ratio=float(max_ratio),
                timeout_s=120,
            )
            actual2 = measure_wav_seconds(out2)
            if actual2 <= tgt * (1.0 + float(tolerance)):
                return out2, PacingReport(
                    target_s=tgt,
                    actual_s=actual2,
                    ratio=compute_ratio(actual2, tgt),
                    action="stretch",
                )
            # still too long => trim hard cap
            out3 = tts_wav.with_suffix(".pacing.trim.wav")
            out3 = pad_or_trim_wav(out2, out3, tgt, timeout_s=120)
            actual3 = measure_wav_seconds(out3)
            return out3, PacingReport(
                target_s=tgt,
                actual_s=actual3,
                ratio=compute_ratio(actual3, tgt),
                action="trim",
                note="hard_cap",
            )
        except Exception:
            # fall back to trim only
            out3 = tts_wav.with_suffix(".pacing.trim.wav")
            out3 = pad_or_trim_wav(tts_wav, out3, tgt, timeout_s=120)
            actual3 = measure_wav_seconds(out3)
            return out3, PacingReport(
                target_s=tgt,
                actual_s=actual3,
                ratio=compute_ratio(actual3, tgt),
                action="trim",
                note="stretch_failed",
            )

    # actual < tgt => pad
    outp = tts_wav.with_suffix(".pacing.pad.wav")
    outp = pad_or_trim_wav(tts_wav, outp, tgt, timeout_s=120)
    actualp = measure_wav_seconds(outp)
    return outp, PacingReport(
        target_s=tgt, actual_s=actualp, ratio=compute_ratio(actualp, tgt), action="pad"
    )
