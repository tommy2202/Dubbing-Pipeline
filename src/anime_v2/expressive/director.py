from __future__ import annotations

import json
import math
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from anime_v2.utils.io import atomic_write_text


@dataclass(frozen=True, slots=True)
class DirectorPlan:
    segment_id: int
    strength: float  # 0..1
    rate_mul: float
    pitch_mul: float
    energy_mul: float
    pause_tail_ms: int
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def _rms_wav(path: Path, *, start_s: float, end_s: float) -> float | None:
    """
    Cheap RMS proxy from PCM16 wav. Returns None on unsupported formats.
    """
    p = Path(path)
    try:
        with wave.open(str(p), "rb") as wf:
            sr = wf.getframerate()
            sw = wf.getsampwidth()
            ch = wf.getnchannels()
            if sw != 2 or sr <= 0:
                return None
            n0 = int(max(0.0, start_s) * sr)
            n1 = int(max(0.0, end_s) * sr)
            n1 = max(n0 + 1, min(n1, wf.getnframes()))
            wf.setpos(min(n0, max(0, wf.getnframes() - 1)))
            buf = wf.readframes(n1 - n0)
        if ch != 1:
            # we only support mono quickly; caller can pass mono16k source_audio_wav
            return None
        n = len(buf) // 2
        if n <= 0:
            return 0.0
        s2 = 0.0
        for i in range(0, n * 2, 2):
            v = int.from_bytes(buf[i : i + 2], "little", signed=True)
            x = float(v) / 32768.0
            s2 += x * x
        return math.sqrt(s2 / float(n))
    except Exception:
        return None


def plan_for_segment(
    *,
    segment_id: int,
    text: str,
    start_s: float,
    end_s: float,
    source_audio_wav: Path | None,
    strength: float,
) -> DirectorPlan:
    """
    Dub Director plan: conservative expressive adjustments based on:
    - punctuation / intent cues
    - scene intensity proxy (RMS from source audio segment) when available

    Must remain compatible with Tier-1 pacing (final duration enforcement happens later).
    """
    st = _clamp(float(strength), 0.0, 1.0)
    t = str(text or "")
    dur = max(0.05, float(end_s) - float(start_s))

    reasons: list[str] = []
    rate = 1.0
    pitch = 1.0
    energy = 1.0
    pause = 0

    # Punctuation cues
    if "!" in t:
        reasons.append("punct:!")
        energy *= 1.0 + 0.10 * st
        pitch *= 1.0 + 0.05 * st
    if "?" in t:
        reasons.append("punct:?")
        pitch *= 1.0 + 0.04 * st
        pause = max(pause, int(70 * st))
    if "..." in t or "…" in t:
        reasons.append("punct:ellipsis")
        rate *= 1.0 - 0.06 * st
        pause = max(pause, int(140 * st))

    low = t.lower()
    # intent-ish keywords (very small deterministic list)
    if any(k in low for k in ["whisper", "quietly", "softly"]):
        reasons.append("intent:quiet")
        energy *= 1.0 - 0.12 * st
        rate *= 1.0 - 0.04 * st
    if any(k in low for k in ["shout", "yell", "scream"]):
        reasons.append("intent:loud")
        energy *= 1.0 + 0.14 * st
        pitch *= 1.0 + 0.05 * st
    if any(k in low for k in ["sorry", "please", "thank you"]):
        reasons.append("intent:polite")
        rate *= 1.0 - 0.03 * st
        pause = max(pause, int(60 * st))

    # Scene intensity proxy from audio (if available)
    rms = None
    if source_audio_wav is not None:
        rms = _rms_wav(Path(source_audio_wav), start_s=float(start_s), end_s=float(end_s))
    if rms is not None:
        # Normalize roughly: ~0.01 quiet, ~0.08 loud (depends on extraction)
        if rms >= 0.06:
            reasons.append("scene:intense")
            energy *= 1.0 + 0.10 * st
            rate *= 1.0 + 0.04 * st
        elif rms <= 0.015:
            reasons.append("scene:calm")
            energy *= 1.0 - 0.08 * st
            rate *= 1.0 - 0.03 * st

    # Clamp to safe bounds (pacing will still enforce duration later)
    rate = _clamp(rate, 0.90, 1.12)
    pitch = _clamp(pitch, 0.92, 1.12)
    energy = _clamp(energy, 0.85, 1.20)
    pause = int(_clamp(float(pause), 0.0, 250.0))

    # If duration is extremely short, avoid adding pause tails
    if dur <= 0.25:
        pause = 0

    return DirectorPlan(
        segment_id=int(segment_id),
        strength=float(st),
        rate_mul=float(rate),
        pitch_mul=float(pitch),
        energy_mul=float(energy),
        pause_tail_ms=int(pause),
        reasons=reasons,
    )


def write_director_plans_jsonl(plans: list[DirectorPlan], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(p.to_dict(), sort_keys=True) for p in plans]
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
