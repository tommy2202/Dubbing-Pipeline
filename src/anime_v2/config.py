from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Single source of truth for config & secrets.

    - Loads from env (and `.env` if present).
    - Secrets use SecretStr so repr() won’t leak.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # security / auth
    jwt_secret: SecretStr = Field(default=SecretStr("dev-insecure-jwt-secret"), alias="JWT_SECRET")
    jwt_alg: str = Field(default="HS256", alias="JWT_ALG")
    csrf_secret: SecretStr = Field(default=SecretStr("dev-insecure-csrf-secret"), alias="CSRF_SECRET")
    session_secret: SecretStr = Field(default=SecretStr("dev-insecure-session-secret"), alias="SESSION_SECRET")
    access_token_minutes: int = Field(default=15, alias="ACCESS_TOKEN_MINUTES")
    refresh_token_days: int = Field(default=7, alias="REFRESH_TOKEN_DAYS")

    cors_origins: str = Field(default="", alias="CORS_ORIGINS")  # comma-separated
    cookie_secure: bool = Field(default=False, alias="COOKIE_SECURE")
    redis_url: str | None = Field(default=None, alias="REDIS_URL")

    admin_username: str | None = Field(default=None, alias="ADMIN_USERNAME")
    admin_password: SecretStr | None = Field(default=None, alias="ADMIN_PASSWORD")

    # egress/offline controls
    offline_mode: bool = Field(default=False, alias="OFFLINE_MODE")
    allow_egress: bool = Field(default=True, alias="ALLOW_EGRESS")
    allow_hf_egress: bool = Field(default=False, alias="ALLOW_HF_EGRESS")

    # pipeline
    whisper_model: str = Field(default="medium", alias="WHISPER_MODEL")
    hf_token: SecretStr | None = Field(default=None, alias="HUGGINGFACE_TOKEN")

    hf_home: Path = Field(default=Path.home() / ".cache" / "huggingface", alias="HF_HOME")
    torch_home: Path = Field(default=Path.home() / ".cache" / "torch", alias="TORCH_HOME")
    tts_home: Path = Field(default=Path.home() / ".local" / "share" / "tts", alias="TTS_HOME")

    enable_pyannote: bool = Field(default=False, alias="ENABLE_PYANNOTE")
    char_sim_thresh: float = Field(default=0.72, alias="CHAR_SIM_THRESH")
    mt_lowconf_thresh: float = Field(default=-0.45, alias="MT_LOWCONF_THRESH")

    mix_profile: str = Field(default="streaming", alias="MIX_PROFILE")
    emit_formats: str = Field(default="mkv,mp4", alias="EMIT_FORMATS")

    tts_model: str = Field(default="tts_models/multilingual/multi-dataset/xtts_v2", alias="TTS_MODEL")
    tts_lang: str = Field(default="en", alias="TTS_LANG")
    tts_speaker: str = Field(default="default", alias="TTS_SPEAKER")
    coqui_tos_agreed: bool = Field(default=False, alias="COQUI_TOS_AGREED")

    translation_model: str | None = Field(default=None, alias="TRANSLATION_MODEL")
    transformers_cache: Path | None = Field(default=None, alias="TRANSFORMERS_CACHE")

    tts_speaker_wav: Path | None = Field(default=None, alias="TTS_SPEAKER_WAV")
    voice_preset_dir: Path = Field(default=Path.cwd() / "voices" / "presets", alias="VOICE_PRESET_DIR")
    voice_db_path: Path = Field(default=Path.cwd() / "voices" / "presets.json", alias="VOICE_DB")

    # character store encryption + ops retention
    char_store_key: SecretStr | None = Field(default=None, alias="CHAR_STORE_KEY")  # 32-byte base64
    char_store_key_file: Path = Field(default=Path.cwd() / "secrets" / "char_store.key", alias="CHAR_STORE_KEY_FILE")
    retention_days_input: int = Field(default=7, alias="RETENTION_DAYS_INPUT")
    retention_days_logs: int = Field(default=14, alias="RETENTION_DAYS_LOGS")

    # runtime model manager / allocator
    prewarm_whisper: str = Field(default="", alias="PREWARM_WHISPER")  # comma-separated models
    prewarm_tts: str = Field(default="", alias="PREWARM_TTS")  # comma-separated models
    model_cache_max: int = Field(default=3, alias="MODEL_CACHE_MAX")
    gpu_util_max: float = Field(default=0.85, alias="GPU_UTIL_MAX")
    gpu_mem_max_ratio: float = Field(default=0.90, alias="GPU_MEM_MAX_RATIO")

    # runtime scheduler (in-proc) limits
    max_concurrency_global: int = Field(default=2, alias="MAX_CONCURRENCY_GLOBAL")
    max_concurrency_transcribe: int = Field(default=1, alias="MAX_CONCURRENCY_TRANSCRIBE")
    max_concurrency_tts: int = Field(default=1, alias="MAX_CONCURRENCY_TTS")
    backpressure_q_max: int = Field(default=6, alias="BACKPRESSURE_Q_MAX")

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in (self.cors_origins or "").split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

