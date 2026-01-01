from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_app_root() -> Path:
    """
    Default application root.

    Historically, this project assumes:
      - Docker: /app
      - Local/dev: current working directory
    """
    env = os.environ.get("APP_ROOT")
    if env:
        return Path(env).resolve()
    if Path("/app").exists():
        return Path("/app").resolve()
    return Path.cwd().resolve()


class PublicConfig(BaseSettings):
    """
    Non-sensitive config with safe defaults.

    Loaded from (in order):
      - process env
      - optional `.env` file
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- core paths ---
    app_root: Path = Field(default_factory=_default_app_root, alias="APP_ROOT")
    output_dir: Path = Field(
        default_factory=lambda: (Path.cwd() / "Output").resolve(), alias="ANIME_V2_OUTPUT_DIR"
    )
    log_dir: Path = Field(
        default_factory=lambda: (Path.cwd() / "logs").resolve(), alias="ANIME_V2_LOG_DIR"
    )
    cache_dir: Path | None = Field(default=None, alias="ANIME_V2_CACHE_DIR")
    models_dir: Path = Field(default=Path("/models"), alias="MODELS_DIR")

    # --- legacy (root `main.py`) paths ---
    uploads_dir: Path = Field(
        default_factory=lambda: (Path.cwd() / "uploads").resolve(),
        alias="UPLOADS_DIR",
    )
    outputs_dir: Path = Field(
        default_factory=lambda: (Path.cwd() / "outputs").resolve(),
        alias="OUTPUTS_DIR",
    )

    # --- tool binaries ---
    ffmpeg_bin: str = Field(default="ffmpeg", alias="FFMPEG_BIN")
    ffprobe_bin: str = Field(default="ffprobe", alias="FFPROBE_BIN")

    # --- per-user settings storage ---
    user_settings_path: Path | None = Field(default=None, alias="ANIME_V2_SETTINGS_PATH")

    # --- alignment / metadata ---
    whisper_word_timestamps: bool = Field(default=False, alias="WHISPER_WORD_TIMESTAMPS")

    # --- expressive speech controls (optional) ---
    emotion_mode: str = Field(default="off", alias="EMOTION_MODE")  # off|auto|tags
    speech_rate: float = Field(default=1.0, alias="SPEECH_RATE")  # global multiplier
    pitch: float = Field(default=1.0, alias="PITCH")  # global multiplier
    energy: float = Field(default=1.0, alias="ENERGY")  # volume multiplier

    # --- web server ---
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")

    # --- logging ---
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_max_bytes: int = Field(default=5 * 1024 * 1024, alias="LOG_MAX_BYTES")
    log_backup_count: int = Field(default=3, alias="LOG_BACKUP_COUNT")

    # --- auth/session behavior (non-secret toggles) ---
    jwt_alg: str = Field(default="HS256", alias="JWT_ALG")
    access_token_minutes: int = Field(default=15, alias="ACCESS_TOKEN_MINUTES")
    refresh_token_days: int = Field(default=7, alias="REFRESH_TOKEN_DAYS")
    cors_origins: str = Field(default="", alias="CORS_ORIGINS")
    cookie_secure: bool = Field(default=False, alias="COOKIE_SECURE")

    # --- egress/offline controls ---
    offline_mode: bool = Field(default=False, alias="OFFLINE_MODE")
    allow_egress: bool = Field(default=True, alias="ALLOW_EGRESS")
    allow_hf_egress: bool = Field(default=False, alias="ALLOW_HF_EGRESS")

    # --- pipeline defaults ---
    whisper_model: str = Field(default="medium", alias="WHISPER_MODEL")

    # cache dirs (non-sensitive)
    hf_home: Path = Field(
        default_factory=lambda: (Path.home() / ".cache" / "huggingface").resolve(), alias="HF_HOME"
    )
    torch_home: Path = Field(
        default_factory=lambda: (Path.home() / ".cache" / "torch").resolve(), alias="TORCH_HOME"
    )
    tts_home: Path = Field(
        default_factory=lambda: (Path.home() / ".local" / "share" / "tts").resolve(),
        alias="TTS_HOME",
    )

    enable_pyannote: bool = Field(default=False, alias="ENABLE_PYANNOTE")
    diarizer: str = Field(default="auto", alias="DIARIZER")
    show_id: str | None = Field(default=None, alias="SHOW_ID")
    char_sim_thresh: float = Field(default=0.72, alias="CHAR_SIM_THRESH")
    mt_lowconf_thresh: float = Field(default=-0.45, alias="MT_LOWCONF_THRESH")
    mt_engine: str = Field(default="auto", alias="MT_ENGINE")
    glossary_path: str | None = Field(default=None, alias="GLOSSARY")
    style_path: str | None = Field(default=None, alias="STYLE")

    mix_profile: str = Field(default="streaming", alias="MIX_PROFILE")
    emit_formats: str = Field(default="mkv,mp4", alias="EMIT_FORMATS")
    separate_vocals: bool = Field(default=False, alias="SEPARATE_VOCALS")
    enable_demucs: bool = Field(default=False, alias="ENABLE_DEMUCS")

    # Tier-1 A: dialogue isolation + enhanced mixing (opt-in; defaults preserve current behavior)
    separation: str = Field(default="off", alias="SEPARATION")  # off|demucs
    separation_model: str = Field(default="htdemucs", alias="SEPARATION_MODEL")
    separation_device: str = Field(default="auto", alias="SEPARATION_DEVICE")  # auto|cpu|cuda
    mix_mode: str = Field(default="legacy", alias="MIX")  # legacy|enhanced
    lufs_target: float = Field(default=-16.0, alias="LUFS_TARGET")
    ducking: bool = Field(default=True, alias="DUCKING")  # used when MIX=enhanced
    ducking_strength: float = Field(default=1.0, alias="DUCKING_STRENGTH")
    limiter: bool = Field(default=True, alias="LIMITER")

    # Tier-1 B/C: timing-aware translation + segment pacing (opt-in; defaults preserve behavior)
    timing_fit: bool = Field(default=False, alias="TIMING_FIT")  # off by default
    pacing: bool = Field(default=False, alias="PACING")  # off by default
    pacing_min_ratio: float = Field(default=0.88, alias="PACING_MIN_STRETCH")
    pacing_max_ratio: float = Field(default=1.18, alias="PACING_MAX_STRETCH")
    timing_wps: float = Field(default=2.7, alias="TIMING_WPS")
    timing_tolerance: float = Field(default=0.10, alias="TIMING_TOLERANCE")
    timing_debug: bool = Field(default=False, alias="TIMING_DEBUG")
    subs_use_fitted_text: bool = Field(default=True, alias="SUBS_USE_FITTED_TEXT")

    tts_model: str = Field(
        default="tts_models/multilingual/multi-dataset/xtts_v2", alias="TTS_MODEL"
    )
    tts_lang: str = Field(default="en", alias="TTS_LANG")
    tts_speaker: str = Field(default="default", alias="TTS_SPEAKER")
    coqui_tos_agreed: bool = Field(default=False, alias="COQUI_TOS_AGREED")
    tts_basic_model: str = Field(
        default="tts_models/en/ljspeech/tacotron2-DDC", alias="TTS_BASIC_MODEL"
    )
    max_stretch: float = Field(default=0.15, alias="MAX_STRETCH")

    translation_model: str | None = Field(default=None, alias="TRANSLATION_MODEL")
    transformers_cache: Path | None = Field(default=None, alias="TRANSFORMERS_CACHE")

    tts_speaker_wav: Path | None = Field(default=None, alias="TTS_SPEAKER_WAV")
    voice_preset_dir: Path = Field(
        default_factory=lambda: (Path.cwd() / "voices" / "presets").resolve(),
        alias="VOICE_PRESET_DIR",
    )
    voice_db_path: Path = Field(
        default_factory=lambda: (Path.cwd() / "voices" / "presets.json").resolve(), alias="VOICE_DB"
    )

    # Voice map helpers (optional)
    voice_map_path: str | None = Field(default=None, alias="VOICE_MAP")
    voice_bank_map_path: str | None = Field(default=None, alias="VOICE_BANK_MAP")
    voice_map_json: str | None = Field(default=None, alias="VOICE_MAP_JSON")
    speaker_signature: str | None = Field(default=None, alias="SPEAKER_SIGNATURE")

    # Voice routing controls (optional)
    voice_mode: str = Field(default="clone", alias="VOICE_MODE")  # clone|preset|single
    voice_ref_dir: Path | None = Field(default=None, alias="VOICE_REF_DIR")
    voice_store_dir: Path = Field(
        default_factory=lambda: (Path.cwd() / "data" / "voices").resolve(),
        alias="VOICE_STORE",
    )

    # Provider selection (F9)
    tts_provider: str = Field(default="auto", alias="TTS_PROVIDER")  # auto|xtts|basic|espeak

    # --- ops: retention/cleanup ---
    # latency budgets (seconds): mark jobs "degraded" when exceeded
    budget_transcribe_sec: int = Field(default=600, alias="BUDGET_TRANSCRIBE_SEC")
    budget_tts_sec: int = Field(default=900, alias="BUDGET_TTS_SEC")
    budget_mux_sec: int = Field(default=120, alias="BUDGET_MUX_SEC")

    retention_days_input: int = Field(default=7, alias="RETENTION_DAYS_INPUT")
    retention_days_logs: int = Field(default=14, alias="RETENTION_DAYS_LOGS")
    char_store_key_file: Path = Field(
        default_factory=lambda: (Path.cwd() / "secrets" / "char_store.key").resolve(),
        alias="CHAR_STORE_KEY_FILE",
    )
    min_free_gb: int = Field(default=10, alias="MIN_FREE_GB")
    work_stale_max_hours: int = Field(default=24, alias="WORK_STALE_MAX_HOURS")

    # --- scheduler / concurrency ---
    jobs_concurrency: int = Field(default=1, alias="JOBS_CONCURRENCY")
    work_prune_interval_sec: int = Field(default=3600, alias="WORK_PRUNE_INTERVAL_SEC")
    drain_timeout_sec: int = Field(default=120, alias="DRAIN_TIMEOUT_SEC")

    max_concurrency_global: int = Field(default=2, alias="MAX_CONCURRENCY_GLOBAL")
    max_concurrency_transcribe: int = Field(default=1, alias="MAX_CONCURRENCY_TRANSCRIBE")
    max_concurrency_tts: int = Field(default=1, alias="MAX_CONCURRENCY_TTS")
    backpressure_q_max: int = Field(default=6, alias="BACKPRESSURE_Q_MAX")

    # runtime model manager / allocator
    prewarm_whisper: str = Field(default="", alias="PREWARM_WHISPER")  # comma-separated models
    prewarm_tts: str = Field(default="", alias="PREWARM_TTS")  # comma-separated models
    model_cache_max: int = Field(default=3, alias="MODEL_CACHE_MAX")
    gpu_util_max: float = Field(default=0.85, alias="GPU_UTIL_MAX")
    gpu_mem_max_ratio: float = Field(default=0.90, alias="GPU_MEM_MAX_RATIO")

    # --- job submission idempotency ---
    idempotency_ttl_sec: int = Field(default=86400, alias="IDEMPOTENCY_TTL_SEC")

    # --- job limits/watchdogs ---
    max_video_min: int = Field(default=120, alias="MAX_VIDEO_MIN")
    max_upload_mb: int = Field(default=2048, alias="MAX_UPLOAD_MB")
    max_concurrent_per_user: int = Field(default=2, alias="MAX_CONCURRENT")
    daily_processing_minutes: int = Field(default=240, alias="DAILY_PROCESSING_MINUTES")

    watchdog_audio_s: int = Field(default=10 * 60, alias="WATCHDOG_AUDIO_S")
    watchdog_diarize_s: int = Field(default=20 * 60, alias="WATCHDOG_DIARIZE_S")
    watchdog_whisper_s: int = Field(default=45 * 60, alias="WATCHDOG_WHISPER_S")
    watchdog_translate_s: int = Field(default=10 * 60, alias="WATCHDOG_TRANSLATE_S")
    watchdog_tts_s: int = Field(default=30 * 60, alias="WATCHDOG_TTS_S")
    watchdog_mix_s: int = Field(default=20 * 60, alias="WATCHDOG_MIX_S")

    # --- retry / circuit breaker ---
    retry_max: int = Field(default=3, alias="RETRY_MAX")
    retry_base_sec: float = Field(default=0.5, alias="RETRY_BASE_SEC")
    retry_cap_sec: float = Field(default=8.0, alias="RETRY_CAP_SEC")
    cb_fail_threshold: int = Field(default=5, alias="CB_FAIL_THRESHOLD")
    cb_cooldown_sec: int = Field(default=60, alias="CB_COOLDOWN_SEC")

    # --- OTEL (opt-in, treated as non-sensitive) ---
    otel_exporter_otlp_endpoint: str | None = Field(
        default=None, alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    otel_service_name: str = Field(default="anime_v2", alias="OTEL_SERVICE_NAME")

    # --- WebRTC defaults (TURN creds live in secrets) ---
    webrtc_stun: str = Field(default="stun:stun.l.google.com:19302", alias="WEBRTC_STUN")
    webrtc_idle_timeout_s: int = Field(default=300, alias="WEBRTC_IDLE_TIMEOUT_S")
    webrtc_max_pcs_per_ip: int = Field(default=2, alias="WEBRTC_MAX_PCS_PER_IP")

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in (self.cors_origins or "").split(",") if o.strip()]
