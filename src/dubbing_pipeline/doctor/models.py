from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.modes import HardwareCaps, resolve_effective_settings
from dubbing_pipeline.plugins.lipsync.wav2lip_plugin import Wav2LipPlugin
from dubbing_pipeline.utils.doctor_types import CheckResult

from dubbing_pipeline.api.routes_system import (  # noqa: E402
    _can_import,
    _secret_configured,
    _whisper_cache_dirs,
    _whisper_model_cached,
)


@dataclass(frozen=True, slots=True)
class ModelRequirement:
    id: str
    name: str
    detect: Callable[[], tuple[bool, dict[str, Any]]]
    remediation: list[str]
    required: bool
    optional_reason: str | None = None


def _norm_mode(mode: str | None) -> str:
    m = str(mode or "").strip().lower()
    if m in {"high", "medium", "low"}:
        return m
    return "high"


def selected_pipeline_mode() -> str:
    for key in ("DUBBING_DOCTOR_MODE", "DUBBING_MODE", "PIPELINE_MODE"):
        val = os.environ.get(key)
        if val:
            return _norm_mode(val)
    return "high"


def _tts_model_cached(model_name: str, tts_home: Path) -> bool:
    root = Path(tts_home).expanduser().resolve()
    if not root.exists():
        return False
    direct = root / model_name
    if direct.exists():
        return True
    alt = root / model_name.replace("/", "--")
    if alt.exists():
        return True
    legacy = root / "tts_models" / model_name
    if legacy.exists():
        return True
    return False


def _base_settings() -> dict[str, Any]:
    s = get_settings()
    return {
        "diarizer": str(getattr(s, "diarizer", "auto") or "auto"),
        "speaker_smoothing": bool(getattr(s, "speaker_smoothing", False)),
        "voice_memory": bool(getattr(s, "voice_memory", False)),
        "voice_mode": str(getattr(s, "voice_mode", "clone") or "clone"),
        "music_detect": bool(getattr(s, "music_detect", False)),
        "separation": str(getattr(s, "separation", "off") or "off"),
        "mix_mode": str(getattr(s, "mix_mode", "legacy") or "legacy"),
        "timing_fit": bool(getattr(s, "timing_fit", False)),
        "pacing": bool(getattr(s, "pacing", False)),
        "qa": False,
        "director": bool(getattr(s, "director", False)),
        "multitrack": bool(getattr(s, "multitrack", False)),
    }


def _requirement_check(req: ModelRequirement, *, mode: str) -> CheckResult:
    ok, details = req.detect()
    details = {**details, "mode": mode, "required": bool(req.required)}
    if ok:
        status = "PASS"
        remediation: list[str] = []
    else:
        status = "FAIL" if req.required else "WARN"
        remediation = list(req.remediation or [])
        if not req.required and req.optional_reason:
            details["fallback"] = str(req.optional_reason)
            details["optional_reason"] = str(req.optional_reason)
    return CheckResult(
        id=req.id,
        name=req.name,
        status=status,
        details=details,
        remediation=remediation,
    )


def _whisper_requirements(*, mode: str, eff_asr_model: str, allow_egress: bool) -> list[ModelRequirement]:
    model_name = str(eff_asr_model or "medium")
    required = mode == "high"

    def _detect_pkg() -> tuple[bool, dict[str, Any]]:
        installed = _can_import("whisper")
        return bool(installed), {"installed": bool(installed)}

    def _detect_weights() -> tuple[bool, dict[str, Any]]:
        cached = _whisper_model_cached(model_name)
        return bool(cached), {
            "model": model_name,
            "cached": bool(cached),
            "allow_egress": bool(allow_egress),
            "cache_dirs": [str(p) for p in _whisper_cache_dirs()],
        }

    return [
        ModelRequirement(
            id="whisper_pkg",
            name="Whisper package installed",
            detect=_detect_pkg,
            remediation=["python3 -m pip install openai-whisper"],
            required=required,
            optional_reason="Pipeline will run in degraded mode without ASR.",
        ),
        ModelRequirement(
            id=f"whisper_weights_{model_name}",
            name=f"Whisper weights cached ({model_name})",
            detect=_detect_weights,
            remediation=[
                f"python3 -c \"import whisper; whisper.load_model('{model_name}')\"",
                "python3 scripts/download_models.py",
            ],
            required=required,
            optional_reason="Missing weights will trigger first-run downloads if egress is allowed.",
        ),
    ]


def _xtts_requirements(*, mode: str, tts_model: str, voice_mode: str) -> list[ModelRequirement]:
    required = mode == "high" and str(voice_mode).lower() == "clone"
    s = get_settings()
    tts_provider = str(getattr(s, "tts_provider", "auto") or "auto").lower()
    coqui_ok = bool(getattr(s, "coqui_tos_agreed", False))
    tts_home = Path(getattr(s, "tts_home", Path.home() / ".local" / "share" / "tts"))

    def _detect_pkg() -> tuple[bool, dict[str, Any]]:
        installed = _can_import("TTS")
        provider_ok = tts_provider in {"auto", "xtts"}
        return bool(installed and provider_ok and coqui_ok), {
            "tts_provider": tts_provider,
            "coqui_tos_agreed": bool(coqui_ok),
            "installed": bool(installed),
        }

    def _detect_weights() -> tuple[bool, dict[str, Any]]:
        cached = _tts_model_cached(tts_model, tts_home)
        return bool(cached), {
            "model": str(tts_model),
            "cached": bool(cached),
            "tts_home": str(tts_home),
        }

    return [
        ModelRequirement(
            id="xtts_prereqs",
            name="XTTS prerequisites (TTS_PROVIDER + COQUI_TOS + package)",
            detect=_detect_pkg,
            remediation=[
                "export TTS_PROVIDER=auto",
                "export COQUI_TOS_AGREED=1",
                "python3 -m pip install TTS",
            ],
            required=required,
            optional_reason="Fallback to basic/espeak TTS may be used if XTTS is disabled.",
        ),
        ModelRequirement(
            id="xtts_weights",
            name=f"XTTS weights cached ({tts_model})",
            detect=_detect_weights,
            remediation=[
                f"python3 -c \"from TTS.api import TTS; TTS('{tts_model}')\"",
            ],
            required=required,
            optional_reason="XTTS will download weights on first use if egress is allowed.",
        ),
    ]


def _wav2lip_requirements(*, mode: str, lipsync_enabled: bool) -> list[ModelRequirement]:
    if not lipsync_enabled:
        return []
    required = mode == "high"

    def _detect() -> tuple[bool, dict[str, Any]]:
        ok = Wav2LipPlugin().is_available()
        return bool(ok), {"available": bool(ok)}

    return [
        ModelRequirement(
            id="wav2lip_weights",
            name="Wav2Lip weights present",
            detect=_detect,
            remediation=["python3 scripts/download_models.py"],
            required=required,
            optional_reason="Lipsync will be skipped if weights are missing.",
        )
    ]


def _demucs_requirements(*, mode: str, separation: str) -> list[ModelRequirement]:
    if str(separation).lower() != "demucs":
        return []

    def _detect() -> tuple[bool, dict[str, Any]]:
        installed = _can_import("demucs")
        return bool(installed), {"installed": bool(installed)}

    return [
        ModelRequirement(
            id="demucs_pkg",
            name="Demucs separation available",
            detect=_detect,
            remediation=["python3 -m pip install demucs"],
            required=False,
            optional_reason="Separation will fall back to off if Demucs is unavailable.",
        )
    ]


def _diarization_requirements(*, mode: str, diarizer: str, enable_pyannote: bool) -> list[ModelRequirement]:
    diar = str(diarizer or "auto").lower()
    reqs: list[ModelRequirement] = []
    if diar == "off":
        return reqs

    if diar == "pyannote" or enable_pyannote:
        def _detect_pyannote() -> tuple[bool, dict[str, Any]]:
            installed = _can_import("pyannote.audio")
            token_ok = _secret_configured(
                getattr(get_settings(), "huggingface_token", None)
                or getattr(get_settings(), "hf_token", None)
            )
            return bool(installed and token_ok), {
                "installed": bool(installed),
                "token_configured": bool(token_ok),
                "diarizer": diar,
            }

        reqs.append(
            ModelRequirement(
                id="pyannote_pkg",
                name="Pyannote diarization available",
                detect=_detect_pyannote,
                remediation=[
                    "export HUGGINGFACE_TOKEN=...  # or HF_TOKEN",
                    "export ENABLE_PYANNOTE=1",
                    "python3 -m pip install pyannote.audio",
                ],
                required=False,
                optional_reason="Diarization will fall back to speechbrain/heuristic if pyannote is unavailable.",
            )
        )
        return reqs

    if diar in {"speechbrain", "auto"}:
        def _detect_speechbrain() -> tuple[bool, dict[str, Any]]:
            installed = _can_import("speechbrain")
            return bool(installed), {"installed": bool(installed), "diarizer": diar}

        reqs.append(
            ModelRequirement(
                id="speechbrain_pkg",
                name="Speechbrain diarization available",
                detect=_detect_speechbrain,
                remediation=["python3 -m pip install speechbrain"],
                required=False,
                optional_reason="Diarization will fall back to heuristic if speechbrain is unavailable.",
            )
        )
    return reqs


def build_model_requirement_checks(mode: str | None = None) -> list[Callable[[], CheckResult]]:
    m = _norm_mode(mode)
    s = get_settings()
    allow_egress = bool(getattr(s, "allow_egress", True))
    caps = HardwareCaps.detect()
    base = _base_settings()
    eff = resolve_effective_settings(mode=m, base=base, overrides={}, caps=caps)
    tts_model = str(getattr(s, "tts_model", "tts_models/multilingual/multi-dataset/xtts_v2") or "")
    voice_mode = str(eff.voice_mode or "clone")
    lipsync_enabled = str(getattr(s, "lipsync", "off") or "off").lower() != "off"
    diarizer = str(eff.diarizer or getattr(s, "diarizer", "auto") or "auto")
    enable_pyannote = bool(getattr(s, "enable_pyannote", False))

    reqs: list[ModelRequirement] = []
    reqs += _whisper_requirements(mode=m, eff_asr_model=str(eff.asr_model), allow_egress=allow_egress)
    reqs += _xtts_requirements(mode=m, tts_model=tts_model, voice_mode=voice_mode)
    reqs += _wav2lip_requirements(mode=m, lipsync_enabled=lipsync_enabled)
    reqs += _demucs_requirements(mode=m, separation=eff.separation)
    reqs += _diarization_requirements(mode=m, diarizer=diarizer, enable_pyannote=enable_pyannote)

    checks: list[Callable[[], CheckResult]] = []
    for req in reqs:
        checks.append(lambda req=req, m=m: _requirement_check(req, mode=m))
    return checks
