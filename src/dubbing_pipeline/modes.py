from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from dubbing_pipeline.utils.log import logger

ModeName = Literal["high", "medium", "low"]


@dataclass(frozen=True, slots=True)
class HardwareCaps:
    gpu_available: bool
    has_demucs: bool
    has_whisper: bool
    has_coqui_tts: bool
    has_pyannote: bool

    @classmethod
    def detect(cls) -> HardwareCaps:
        def _can_import(mod: str) -> bool:
            try:
                __import__(mod)
                return True
            except Exception:
                return False

        gpu = False
        try:
            import torch  # type: ignore

            gpu = bool(torch.cuda.is_available())
        except Exception:
            gpu = False

        return cls(
            gpu_available=gpu,
            has_demucs=_can_import("demucs"),
            has_whisper=_can_import("whisper"),
            has_coqui_tts=_can_import("TTS"),
            has_pyannote=_can_import("pyannote.audio"),
        )


@dataclass(frozen=True, slots=True)
class EffectiveSettings:
    requested_mode: ModeName
    effective_mode: ModeName
    caps: dict[str, Any]
    # key effective knobs used across the pipeline
    asr_model: str
    diarizer: str  # auto|pyannote|speechbrain|heuristic|off
    speaker_smoothing: bool
    voice_memory: bool
    voice_mode: str  # clone|preset|single
    music_detect: bool
    separation: str  # off|demucs
    mix_mode: str  # legacy|enhanced
    timing_fit: bool
    pacing: bool
    qa: bool
    director: bool
    multitrack: bool
    stream_context_seconds: float
    # decision trace
    sources: dict[str, str]
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_mode": self.requested_mode,
            "effective_mode": self.effective_mode,
            "caps": self.caps,
            "asr_model": self.asr_model,
            "diarizer": self.diarizer,
            "speaker_smoothing": self.speaker_smoothing,
            "voice_memory": self.voice_memory,
            "voice_mode": self.voice_mode,
            "music_detect": self.music_detect,
            "separation": self.separation,
            "mix_mode": self.mix_mode,
            "timing_fit": self.timing_fit,
            "pacing": self.pacing,
            "qa": self.qa,
            "director": self.director,
            "multitrack": self.multitrack,
            "stream_context_seconds": float(self.stream_context_seconds),
            "sources": dict(self.sources),
            "reasons": list(self.reasons),
        }


def _norm_mode(mode: str | None) -> ModeName:
    m = str(mode or "medium").strip().lower()
    if m not in {"high", "medium", "low"}:
        return "medium"
    return m  # type: ignore[return-value]


def resolve_effective_settings(
    *,
    mode: str | None,
    base: dict[str, Any],
    overrides: dict[str, Any],
    caps: HardwareCaps | None = None,
) -> EffectiveSettings:
    """
    Canonical mode resolver:
      mode defaults -> explicit overrides -> hardware fallbacks (logged)

    - `base` should contain already-parsed values from CLI/web/job runtime.
    - `overrides` should contain *only* explicit user overrides (not defaults).
    """
    caps = caps or HardwareCaps.detect()
    req = _norm_mode(mode)
    eff: ModeName = req
    reasons: list[str] = []
    sources: dict[str, str] = {}

    def pick(name: str, *, mode_default: Any) -> Any:
        if name in overrides:
            sources[name] = "override"
            return overrides[name]
        if req in {"high", "low"}:
            sources[name] = f"mode:{req}"
            return mode_default
        sources[name] = "base"
        return base.get(name)

    # --- mode defaults (only enforced for HIGH/LOW; MEDIUM preserves existing behavior) ---
    diarizer = pick("diarizer", mode_default=("auto" if req != "low" else "off"))
    speaker_smoothing = bool(pick("speaker_smoothing", mode_default=(req == "high")))
    voice_memory = bool(pick("voice_memory", mode_default=(req == "high")))
    voice_mode = str(pick("voice_mode", mode_default=("clone" if req == "high" else "single")))
    music_detect = bool(pick("music_detect", mode_default=False))
    separation = str(pick("separation", mode_default=("demucs" if req == "high" else "off")))
    mix_mode = str(pick("mix_mode", mode_default=("enhanced" if req == "high" else "legacy")))
    timing_fit = bool(pick("timing_fit", mode_default=(req == "high")))
    pacing = bool(pick("pacing", mode_default=(req == "high")))
    qa = bool(pick("qa", mode_default=(req == "high")))
    director = bool(pick("director", mode_default=(req == "high")))
    multitrack = bool(pick("multitrack", mode_default=(req == "high")))
    # Streaming context bridging (Feature I): on by default for HIGH/MED (15s), off for LOW.
    # This is intentionally resolved even when `base` doesn't include the key (contract tests depend on it).
    if "stream_context_seconds" in overrides:
        sources["stream_context_seconds"] = "override"
        stream_context_seconds = float(overrides.get("stream_context_seconds") or 0.0)
    elif req == "low":
        sources["stream_context_seconds"] = "mode:low"
        stream_context_seconds = 0.0
    else:
        if "stream_context_seconds" in base and base.get("stream_context_seconds") is not None:
            sources["stream_context_seconds"] = "base"
            stream_context_seconds = float(base.get("stream_context_seconds") or 0.0)
        else:
            sources["stream_context_seconds"] = f"mode:{req}"
            stream_context_seconds = 15.0

    # ASR model: mode default unless explicitly overridden
    asr_override = overrides.get("asr_model")
    if asr_override:
        asr_model = str(asr_override)
        sources["asr_model"] = "override"
    else:
        sources["asr_model"] = f"mode:{req}"
        if req == "high":
            asr_model = "large-v3" if caps.gpu_available else "medium"
            if not caps.gpu_available:
                reasons.append("high_mode_no_gpu:asr_model=medium")
        elif req == "low":
            # CPU-friendly default; allow further downgrade to tiny via explicit override.
            asr_model = "small"
        else:
            asr_model = "medium"

    # --- hardware fallbacks (only adjust when required, never silently) ---
    if separation.strip().lower() == "demucs" and not caps.has_demucs:
        separation = "off"
        reasons.append("demucs_missing:separation=off")
        sources["separation"] = "fallback"

    # diarizer off is always allowed (fast path); pyannote requires deps
    if str(diarizer).lower() == "pyannote" and not caps.has_pyannote:
        diarizer = "auto"
        reasons.append("pyannote_missing:diarizer=auto")
        sources["diarizer"] = "fallback"

    # voice_memory needs embeddings providers; keep enabled but warn if likely degraded.
    if voice_memory and not (caps.has_coqui_tts or caps.has_whisper or caps.has_pyannote):
        # We don't hard-disable because voice memory can still store refs; but we surface a reason.
        reasons.append("voice_memory_enabled_but_embedding_deps_missing")

    return EffectiveSettings(
        requested_mode=req,
        effective_mode=eff,
        caps={
            "gpu_available": bool(caps.gpu_available),
            "has_demucs": bool(caps.has_demucs),
            "has_whisper": bool(caps.has_whisper),
            "has_coqui_tts": bool(caps.has_coqui_tts),
            "has_pyannote": bool(caps.has_pyannote),
        },
        asr_model=str(asr_model),
        diarizer=str(diarizer).lower(),
        speaker_smoothing=bool(speaker_smoothing),
        voice_memory=bool(voice_memory),
        voice_mode=str(voice_mode).lower(),
        music_detect=bool(music_detect),
        separation=str(separation).lower(),
        mix_mode=str(mix_mode).lower(),
        timing_fit=bool(timing_fit),
        pacing=bool(pacing),
        qa=bool(qa),
        director=bool(director),
        multitrack=bool(multitrack),
        stream_context_seconds=float(max(0.0, stream_context_seconds)),
        sources=sources,
        reasons=reasons,
    )


def log_effective_settings_summary(eff: EffectiveSettings) -> None:
    """
    Single-line-ish structured summary for logs.
    """
    logger.info(
        "effective_settings",
        requested_mode=eff.requested_mode,
        effective_mode=eff.effective_mode,
        asr_model=eff.asr_model,
        diarizer=eff.diarizer,
        speaker_smoothing=eff.speaker_smoothing,
        voice_memory=eff.voice_memory,
        voice_mode=eff.voice_mode,
        separation=eff.separation,
        mix_mode=eff.mix_mode,
        timing_fit=eff.timing_fit,
        pacing=eff.pacing,
        qa=eff.qa,
        director=eff.director,
        multitrack=eff.multitrack,
        stream_context_seconds=float(eff.stream_context_seconds),
        reasons=eff.reasons,
    )
