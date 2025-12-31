from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import log


class ConfigError(ValueError):
    pass


def _env(key: str, default: str | None = None) -> str | None:
    val = os.environ.get(key)
    if val is None:
        return default
    val = val.strip()
    return val if val != "" else default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key, None)
    if raw is None:
        return default
    raw_l = raw.lower()
    if raw_l in {"1", "true", "yes", "y", "on"}:
        return True
    if raw_l in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"{key} must be a boolean-like value, got {raw!r}")


def _validate_choice(key: str, value: str, allowed: set[str]) -> str:
    if value not in allowed:
        allowed_s = ", ".join(sorted(allowed))
        raise ConfigError(f"{key} must be one of: {allowed_s}. Got: {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    api_token: str
    coqui_tos_agreed: bool

    tts_model: str
    tts_lang: str
    tts_speaker: str

    whisper_model: str

    hf_home: Path
    torch_home: Path
    tts_home: Path

    diarization_model: str
    hf_token: str | None

    translation_model: str | None
    transformers_cache: Path | None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Read environment variables with sane defaults and validate.

    Note: this intentionally does not auto-load a `.env` file to avoid adding deps.
    """
    api_token = _env("API_TOKEN", "change-me") or "change-me"
    coqui_tos_agreed = _env_bool("COQUI_TOS_AGREED", default=False)

    tts_model = _env("TTS_MODEL", "tts_models/multilingual/multi-dataset/xtts_v2") or ""
    tts_lang = _env("TTS_LANG", "en") or "en"
    tts_speaker = _env("TTS_SPEAKER", "default") or "default"

    whisper_model = _env("WHISPER_MODEL", "medium") or "medium"
    whisper_model = _validate_choice(
        "WHISPER_MODEL",
        whisper_model,
        allowed={"tiny", "base", "small", "medium", "large", "large-v2", "large-v3"},
    )

    hf_home = Path(_env("HF_HOME", str(Path.home() / ".cache" / "huggingface")) or "")
    torch_home = Path(_env("TORCH_HOME", str(Path.home() / ".cache" / "torch")) or "")
    tts_home = Path(_env("TTS_HOME", str(Path.home() / ".local" / "share" / "tts")) or "")

    diarization_model = _env("DIARIZATION_MODEL", "pyannote/speaker-diarization") or "pyannote/speaker-diarization"
    hf_token = _env("HF_TOKEN", None) or _env("HUGGINGFACE_TOKEN", None)

    translation_model = _env("TRANSLATION_MODEL", None)
    transformers_cache_raw = _env("TRANSFORMERS_CACHE", None) or _env("HF_HUB_CACHE", None)
    transformers_cache = Path(transformers_cache_raw) if transformers_cache_raw else None

    settings = Settings(
        api_token=api_token,
        coqui_tos_agreed=coqui_tos_agreed,
        tts_model=tts_model,
        tts_lang=tts_lang,
        tts_speaker=tts_speaker,
        whisper_model=whisper_model,
        hf_home=hf_home,
        torch_home=torch_home,
        tts_home=tts_home,
        diarization_model=diarization_model,
        hf_token=hf_token,
        translation_model=translation_model,
        transformers_cache=transformers_cache,
    )

    # Soft warnings (don’t block imports/tests)
    if settings.api_token == "change-me":
        log.warning("API_TOKEN is set to default 'change-me' (set it in env for production).")
    if not settings.coqui_tos_agreed:
        log.warning("COQUI_TOS_AGREED is not enabled (set COQUI_TOS_AGREED=1 to acknowledge Coqui TOS).")

    return settings

