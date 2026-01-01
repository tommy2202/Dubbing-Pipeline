from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from anime_v2.expressive.prosody import ProsodyFeatures
from anime_v2.timing.pacing import atempo_chain
from anime_v2.utils.ffmpeg_safe import run_ffmpeg
from anime_v2.utils.log import logger


@dataclass(frozen=True, slots=True)
class ExpressivePlan:
    segment_id: int
    mode: str  # off|auto|source-audio|text-only
    strength: float  # 0..1
    category: str
    rate_mul: float
    pitch_mul: float
    energy_mul: float
    pause_tail_ms: int
    notes: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def plan_for_segment(
    *,
    segment_id: int,
    mode: str,
    strength: float,
    text: str,
    features: ProsodyFeatures | None,
) -> ExpressivePlan:
    """
    Convert features + text cues to conservative synthesis controls.

    These are deliberately small so Tier-1 pacing can still hit target durations.
    """
    m = str(mode or "off").strip().lower()
    if m not in {"off", "auto", "source-audio", "text-only"}:
        m = "off"
    st = _clamp(float(strength), 0.0, 1.0)

    cat = "neutral"
    rms = None
    pitch_hz = None
    if features is not None:
        cat = str(features.category or "neutral")
        rms = features.rms
        pitch_hz = features.pitch_hz

    t = str(text or "")
    has_bang = "!" in t
    has_q = "?" in t
    has_ell = ("..." in t) or ("…" in t)

    # Base multipliers
    rate = 1.0
    pitch = 1.0
    energy = 1.0
    pause_ms = 0

    # Text-only cues (always available)
    if has_bang:
        pitch *= 1.0 + 0.04 * st
        energy *= 1.0 + 0.10 * st
    if has_q:
        pitch *= 1.0 + 0.03 * st
    if has_ell:
        rate *= 1.0 - 0.04 * st
        energy *= 1.0 - 0.08 * st
        pause_ms = int(120 * st)

    # Feature-driven category (only when we have it)
    if m in {"auto", "source-audio"} and features is not None:
        if cat == "excited":
            rate *= 1.0 + 0.04 * st
            pitch *= 1.0 + 0.03 * st
            energy *= 1.0 + 0.12 * st
        elif cat == "angry":
            rate *= 1.0 + 0.03 * st
            pitch *= 1.0 + 0.01 * st
            energy *= 1.0 + 0.18 * st
        elif cat == "sad":
            rate *= 1.0 - 0.04 * st
            pitch *= 1.0 - 0.03 * st
            energy *= 1.0 - 0.15 * st
            pause_ms = max(pause_ms, int(160 * st))
        elif cat == "calm":
            rate *= 1.0 - 0.02 * st
            pitch *= 1.0 - 0.01 * st
            energy *= 1.0 - 0.08 * st

        # Extra nudge by RMS when available (very small)
        if rms is not None:
            if rms > 0.12:
                energy *= 1.0 + 0.05 * st
            elif rms < 0.05:
                energy *= 1.0 - 0.03 * st
        if pitch_hz is not None and pitch_hz > 240:
            pitch *= 1.0 + 0.02 * st

    # Clamp to conservative bounds
    rate = _clamp(rate, 0.90, 1.10)
    pitch = _clamp(pitch, 0.95, 1.07)
    energy = _clamp(energy, 0.85, 1.25)
    pause_ms = int(_clamp(float(pause_ms), 0.0, 220.0))

    return ExpressivePlan(
        segment_id=int(segment_id),
        mode=m,
        strength=float(st),
        category=str(cat),
        rate_mul=float(rate),
        pitch_mul=float(pitch),
        energy_mul=float(energy),
        pause_tail_ms=int(pause_ms),
        notes={"has_bang": has_bang, "has_q": has_q, "has_ell": has_ell},
    )


def write_plan_json(plan: ExpressivePlan, path: Path, *, features: ProsodyFeatures | None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"plan": plan.to_dict(), "features": (features.to_dict() if features else None)}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def apply_prosody_ffmpeg(
    wav_in: Path,
    *,
    ffmpeg_bin: Path,
    rate: float = 1.0,
    pitch: float = 1.0,
    energy: float = 1.0,
) -> Path:
    """
    Lightweight prosody controls via ffmpeg filters (offline).

    - rate: tempo multiplier (1.0 = unchanged)
    - pitch: pitch multiplier (1.0 = unchanged), implemented via asetrate trick
    - energy: volume multiplier (1.0 = unchanged)
    """
    wav_in = Path(wav_in)
    r = _clamp(float(rate), 0.5, 2.0)
    p = _clamp(float(pitch), 0.8, 1.25)
    e = _clamp(float(energy), 0.2, 3.0)
    if abs(r - 1.0) < 0.01 and abs(p - 1.0) < 0.01 and abs(e - 1.0) < 0.01:
        return wav_in

    out = wav_in.with_suffix(".prosody.wav")
    filters: list[str] = []
    if abs(p - 1.0) >= 0.01:
        filters.append(f"asetrate=16000*{p:.4f}")
        filters.append(atempo_chain(1.0 / p))
    if abs(r - 1.0) >= 0.01:
        filters.append(atempo_chain(r))
    if abs(e - 1.0) >= 0.01:
        filters.append(f"volume={e:.3f}")
    filt = ",".join(filters)
    try:
        run_ffmpeg(
            [
                str(ffmpeg_bin),
                "-y",
                "-i",
                str(wav_in),
                "-af",
                filt,
                "-ac",
                "1",
                "-ar",
                "16000",
                str(out),
            ],
            timeout_s=120,
            retries=0,
            capture=True,
        )
        return out
    except Exception as ex:
        logger.warning("expressive_ffmpeg_failed", error=str(ex))
        return wav_in

