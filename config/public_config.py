from __future__ import annotations

import os
from pathlib import Path

from pydantic import AliasChoices, Field
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
        default_factory=lambda: (Path.cwd() / "Output").resolve(), alias="DUBBING_OUTPUT_DIR"
    )
    log_dir: Path = Field(
        default_factory=lambda: (Path.cwd() / "logs").resolve(), alias="DUBBING_LOG_DIR"
    )
    # Runtime-only state directory (DBs, internal state). Prefer a non-repo mount in production.
    # If unset, defaults to "<DUBBING_OUTPUT_DIR>/_state".
    state_dir: Path | None = Field(default=None, alias="DUBBING_STATE_DIR")
    auth_db_name: str = Field(default="auth.db", alias="DUBBING_AUTH_DB_NAME")
    jobs_db_name: str = Field(default="jobs.db", alias="DUBBING_JOBS_DB_NAME")
    cache_dir: Path | None = Field(default=None, alias="DUBBING_CACHE_DIR")
    models_dir: Path = Field(default=Path("/models"), alias="MODELS_DIR")

    # Web/API input layout (uploads)
    # Defaults match the repo's historical container layout: <APP_ROOT>/Input/uploads
    input_dir: Path | None = Field(default=None, alias="INPUT_DIR")
    input_uploads_dir: Path | None = Field(default=None, alias="INPUT_UPLOADS_DIR")
    upload_chunk_bytes: int = Field(default=5 * 1024 * 1024, alias="UPLOAD_CHUNK_BYTES")

    # --- legacy (root `main.py`) paths ---
    uploads_dir: Path = Field(
        default_factory=lambda: (Path.cwd() / "uploads").resolve(),
        alias="UPLOADS_DIR",
    )
    outputs_dir: Path = Field(
        default_factory=lambda: (Path.cwd() / "outputs").resolve(),
        alias="OUTPUTS_DIR",
    )

    # --- legacy defaults (optional; configurable) ---
    legacy_output_dir: Path = Field(default=Path("/data/out"), alias="LEGACY_OUTPUT_DIR")
    legacy_ui_host: str = Field(default="0.0.0.0", alias="LEGACY_HOST")
    legacy_ui_port: int = Field(default=7860, alias="LEGACY_PORT")

    # --- tool binaries ---
    ffmpeg_bin: str = Field(default="ffmpeg", alias="FFMPEG_BIN")
    ffprobe_bin: str = Field(default="ffprobe", alias="FFPROBE_BIN")

    # --- per-user settings storage ---
    user_settings_path: Path | None = Field(default=None, alias="DUBBING_SETTINGS_PATH")

    # --- UI telemetry / audit (optional; off by default to avoid noisy logs) ---
    ui_audit_page_views: bool = Field(default=False, alias="DUBBING_UI_AUDIT_PAGE_VIEWS")

    # --- alignment / metadata ---
    whisper_word_timestamps: bool = Field(default=False, alias="WHISPER_WORD_TIMESTAMPS")

    # --- expressive speech controls (optional) ---
    emotion_mode: str = Field(default="off", alias="EMOTION_MODE")  # off|auto|tags
    speech_rate: float = Field(default=1.0, alias="SPEECH_RATE")  # global multiplier
    pitch: float = Field(default=1.0, alias="PITCH")  # global multiplier
    energy: float = Field(default=1.0, alias="ENERGY")  # volume multiplier

    # Tier-3 B: expressive/prosody controls (opt-in; default off)
    expressive: str = Field(default="off", alias="EXPRESSIVE")  # off|auto|source-audio|text-only
    expressive_strength: float = Field(default=0.5, alias="EXPRESSIVE_STRENGTH")  # 0..1
    expressive_debug: bool = Field(default=False, alias="EXPRESSIVE_DEBUG")

    # Tier-3 C: streaming mode (opt-in; default off)
    stream: bool = Field(default=False, alias="STREAM")  # off by default
    stream_chunk_seconds: float = Field(default=20.0, alias="STREAM_CHUNK_SECONDS")
    stream_overlap_seconds: float = Field(default=2.0, alias="STREAM_OVERLAP_SECONDS")
    stream_context_seconds: float = Field(default=15.0, alias="STREAM_CONTEXT_SECONDS")
    stream_output: str = Field(default="segments", alias="STREAM_OUTPUT")  # segments|final
    stream_concurrency: int = Field(default=1, alias="STREAM_CONCURRENCY")

    # Tier-Next A/B: music/singing preservation (opt-in; default off)
    music_detect: bool = Field(default=False, alias="MUSIC_DETECT")
    music_mode: str = Field(default="auto", alias="MUSIC_MODE")  # auto|heuristic|classifier
    music_threshold: float = Field(default=0.70, alias="MUSIC_THRESHOLD")
    op_ed_detect: bool = Field(default=False, alias="OP_ED_DETECT")
    op_ed_seconds: int = Field(default=90, alias="OP_ED_SECONDS")

    # Tier-Next F: speaker smoothing / audio scene detection (opt-in; default off)
    speaker_smoothing: bool = Field(default=False, alias="SPEAKER_SMOOTHING")
    scene_detect: str = Field(default="audio", alias="SCENE_DETECT")  # off|audio
    smoothing_min_turn_s: float = Field(default=0.6, alias="SMOOTHING_MIN_TURN_S")
    smoothing_surround_gap_s: float = Field(default=0.4, alias="SMOOTHING_SURROUND_GAP_S")

    # Tier-Next G: Dub Director mode (opt-in; default off)
    director: bool = Field(default=False, alias="DIRECTOR")
    director_strength: float = Field(default=0.5, alias="DIRECTOR_STRENGTH")

    # Tier-Next H: multi-track outputs (opt-in; default off)
    multitrack: bool = Field(default=False, alias="MULTITRACK")
    container: str = Field(default="mkv", alias="CONTAINER")  # mkv|mp4 (used when multitrack on)

    # Mobile playback artifacts (does not change master outputs)
    mobile_outputs: bool = Field(default=True, alias="MOBILE_OUTPUTS")
    mobile_hls: bool = Field(default=False, alias="MOBILE_HLS")
    enable_audio_preview: bool = Field(default=False, alias="ENABLE_AUDIO_PREVIEW")
    enable_lowres_preview: bool = Field(default=False, alias="ENABLE_LOWRES_PREVIEW")
    lowres_preview_preset: str = Field(default="480p", alias="LOWRES_PREVIEW_PRESET")

    # Feature B: retention/cache policy (opt-in; default keep everything)
    cache_policy: str = Field(default="full", alias="CACHE_POLICY")  # full|balanced|minimal
    retention_days: int = Field(default=0, alias="RETENTION_DAYS")  # 0 disables time-based pruning

    # --- Security/Privacy vNext: encryption at rest (optional) ---
    # OFF by default. If enabled, selected artifact classes are stored encrypted-at-rest.
    encrypt_at_rest: bool = Field(default=False, alias="ENCRYPT_AT_REST")
    # Comma-separated classes; empty => "all supported sensitive classes".
    # Supported: uploads,audio,transcripts,voice_memory,review,logs
    encrypt_at_rest_classes: str = Field(default="", alias="ENCRYPT_AT_REST_CLASSES")

    # --- Security/Privacy vNext: privacy mode (optional) ---
    # OFF by default. When enabled, minimizes stored intermediates and triggers minimal retention.
    privacy_mode: str = Field(default="off", alias="PRIVACY_MODE")  # off|on
    no_store_transcript: bool = Field(default=False, alias="NO_STORE_TRANSCRIPT")
    no_store_source_audio: bool = Field(default=False, alias="NO_STORE_SOURCE_AUDIO")
    minimal_artifacts: bool = Field(default=False, alias="MINIMAL_ARTIFACTS")

    # --- web server ---
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")

    # --- remote access hardening (mobile) ---
    # off: no IP allowlist enforcement (default for local dev)
    # tailscale: allow only LAN/private + Tailscale CGNAT ranges by default
    # cloudflare: expect a trusted proxy (cloudflared/caddy) and optionally enforce Cloudflare Access JWT
    remote_access_mode: str = Field(default="off", alias="REMOTE_ACCESS_MODE")  # off|tailscale|cloudflare
    allowed_subnets: str = Field(default="", alias="ALLOWED_SUBNETS")  # comma/space separated CIDRs
    trust_proxy_headers: bool = Field(default=False, alias="TRUST_PROXY_HEADERS")
    trusted_proxy_subnets: str = Field(
        default="",
        alias=AliasChoices("TRUSTED_PROXY_SUBNETS", "TRUSTED_PROXIES"),
    )  # CIDRs or IPs

    # Optional Cloudflare Access verification (recommended when REMOTE_ACCESS_MODE=cloudflare)
    # These are not secrets (they identify the Access app), but verification may require fetching JWKS.
    cloudflare_access_team_domain: str | None = Field(
        default=None, alias="CLOUDFLARE_ACCESS_TEAM_DOMAIN"
    )  # e.g. "myteam" (myteam.cloudflareaccess.com)
    cloudflare_access_aud: str | None = Field(default=None, alias="CLOUDFLARE_ACCESS_AUD")

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
    allow_legacy_token_login: bool = Field(
        default=False, alias="ALLOW_LEGACY_TOKEN_LOGIN"
    )  # UNSAFE on public networks
    enable_api_keys: bool = Field(default=True, alias="ENABLE_API_KEYS")

    # Safer auth UX (optional, off by default)
    enable_qr_login: bool = Field(default=False, alias="ENABLE_QR_LOGIN")
    qr_login_ttl_sec: int = Field(default=60, alias="QR_LOGIN_TTL_SEC")

    # Optional TOTP (2FA) for admin accounts only (off by default)
    enable_totp: bool = Field(default=False, alias="ENABLE_TOTP")

    # --- egress/offline controls ---
    offline_mode: bool = Field(default=False, alias="OFFLINE_MODE")
    allow_egress: bool = Field(default=True, alias="ALLOW_EGRESS")
    allow_hf_egress: bool = Field(default=False, alias="ALLOW_HF_EGRESS")

    # --- notifications (optional; private/self-hosted) ---
    # When enabled, the server will POST to a self-hosted ntfy instance on job completion/failure.
    # This is opt-in and safe to leave unconfigured; defaults preserve current behavior.
    ntfy_enabled: bool = Field(default=False, alias="NTFY_ENABLED")
    ntfy_base_url: str = Field(default="", alias="NTFY_BASE_URL")  # e.g. http://127.0.0.1:8081
    ntfy_topic: str = Field(default="", alias="NTFY_TOPIC")  # choose a random topic
    ntfy_tls_insecure: bool = Field(default=False, alias="NTFY_TLS_INSECURE")
    ntfy_timeout_sec: float = Field(default=5.0, alias="NTFY_TIMEOUT_SEC")
    ntfy_retries: int = Field(default=3, alias="NTFY_RETRIES")
    ntfy_notify_admin: bool = Field(default=False, alias="NTFY_NOTIFY_ADMIN")
    ntfy_admin_topic: str = Field(default="", alias="NTFY_ADMIN_TOPIC")

    # Optional public base URL used to generate absolute links in notifications/QRs.
    # If unset, notifications will omit the click URL.
    public_base_url: str = Field(default="", alias="PUBLIC_BASE_URL")

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

    # Feature M: optional offline rewrite/transcreation provider (OFF by default).
    rewrite_provider: str = Field(default="heuristic", alias="REWRITE_PROVIDER")  # heuristic|local_llm
    rewrite_endpoint: str | None = Field(default=None, alias="REWRITE_ENDPOINT")  # localhost only
    rewrite_model: Path | None = Field(default=None, alias="REWRITE_MODEL")  # local path (optional)
    rewrite_strict: bool = Field(default=True, alias="REWRITE_STRICT")

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

    # Speaker reference extraction (post-diarization; used to build per-speaker ~N second refs).
    # This does not enable any new pipeline by itself; it just writes reference WAVs for downstream use.
    voice_ref_target_s: float = Field(
        default=30.0, validation_alias=AliasChoices("VOICE_REF_TARGET_SECONDS", "VOICE_REF_TARGET_S")
    )
    voice_ref_min_candidate_s: float = Field(
        default=0.7,
        validation_alias=AliasChoices("VOICE_REF_MIN_SEG_SECONDS", "VOICE_REF_MIN_CANDIDATE_S"),
    )
    voice_ref_max_candidate_s: float = Field(
        default=12.0,
        validation_alias=AliasChoices("VOICE_REF_MAX_SEG_SECONDS", "VOICE_REF_MAX_CANDIDATE_S"),
    )
    voice_ref_overlap_eps_s: float = Field(default=0.05, alias="VOICE_REF_OVERLAP_EPS_S")
    voice_ref_min_speech_ratio: float = Field(default=0.60, alias="VOICE_REF_MIN_SPEECH_RATIO")

    # Two-pass voice cloning: pass1 runs without cloning to build speaker refs; pass2 reruns TTS+mix using refs.
    voice_clone_two_pass: bool = Field(default=False, alias="VOICE_CLONE_TWO_PASS")

    # Tier-2 A: Character Voice Memory (opt-in; defaults preserve current behavior)
    voice_memory: bool = Field(
        default=False, validation_alias=AliasChoices("VOICE_MEMORY_ENABLED", "VOICE_MEMORY")
    )
    voice_memory_dir: Path = Field(
        default_factory=lambda: (Path.cwd() / "data" / "voice_memory").resolve(),
        alias="VOICE_MEMORY_DIR",
    )
    voice_auto_match: bool = Field(default=False, alias="VOICE_AUTO_MATCH")
    voice_match_threshold: float = Field(default=0.75, alias="VOICE_MATCH_THRESHOLD")
    voice_auto_enroll: bool = Field(default=True, alias="VOICE_AUTO_ENROLL")
    voice_character_map: Path | None = Field(default=None, alias="VOICE_CHARACTER_MAP")

    # Provider selection (F9)
    tts_provider: str = Field(default="auto", alias="TTS_PROVIDER")  # auto|xtts|basic|espeak

    # --- Tier-3 A: lip-sync plugin (optional; default off) ---
    lipsync: str = Field(default="off", alias="LIPSYNC")  # off|wav2lip
    strict_plugins: bool = Field(default=False, alias="STRICT_PLUGINS")
    wav2lip_dir: Path | None = Field(default=None, alias="WAV2LIP_DIR")
    wav2lip_checkpoint: Path | None = Field(default=None, alias="WAV2LIP_CHECKPOINT")
    lipsync_face: str = Field(default="auto", alias="LIPSYNC_FACE")  # auto|center|bbox
    lipsync_device: str = Field(default="auto", alias="LIPSYNC_DEVICE")  # auto|cpu|cuda
    lipsync_box: str | None = Field(default=None, alias="LIPSYNC_BOX")  # "x1,y1,x2,y2"
    lipsync_timeout_s: int = Field(default=1200, alias="LIPSYNC_TIMEOUT_S")
    # Feature J: scene-limited lip-sync (best-effort; default off)
    lipsync_scene_limited: bool = Field(default=False, alias="LIPSYNC_SCENE_LIMITED")
    lipsync_sample_every_s: float = Field(default=0.5, alias="LIPSYNC_SAMPLE_EVERY_S")
    lipsync_min_face_ratio: float = Field(default=0.60, alias="LIPSYNC_MIN_FACE_RATIO")
    lipsync_min_range_s: float = Field(default=2.0, alias="LIPSYNC_MIN_RANGE_S")
    lipsync_merge_gap_s: float = Field(default=0.6, alias="LIPSYNC_MERGE_GAP_S")
    lipsync_max_frames: int = Field(default=600, alias="LIPSYNC_MAX_FRAMES")

    # --- ops: retention/cleanup ---
    # latency budgets (seconds): mark jobs "degraded" when exceeded
    budget_transcribe_sec: int = Field(default=600, alias="BUDGET_TRANSCRIBE_SEC")
    budget_tts_sec: int = Field(default=900, alias="BUDGET_TTS_SEC")
    budget_mux_sec: int = Field(default=120, alias="BUDGET_MUX_SEC")

    retention_enabled: bool = Field(default=True, alias="RETENTION_ENABLED")
    retention_upload_ttl_hours: int = Field(default=24, alias="RETENTION_UPLOAD_TTL_HOURS")
    retention_job_artifact_days: int = Field(default=14, alias="RETENTION_JOB_ARTIFACT_DAYS")
    retention_log_days: int = Field(default=14, alias="RETENTION_LOG_DAYS")
    retention_interval_sec: int = Field(default=3600, alias="RETENTION_INTERVAL_SEC")

    # Legacy retention knobs (kept for compatibility; prefer the RETENTION_* settings above).
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

    # Optional per-mode caps (0 => fall back to MAX_CONCURRENCY_GLOBAL)
    max_jobs_high: int = Field(default=1, alias="MAX_JOBS_HIGH")
    max_jobs_medium: int = Field(default=0, alias="MAX_JOBS_MEDIUM")
    max_jobs_low: int = Field(default=0, alias="MAX_JOBS_LOW")

    # Global cross-instance queue limits (enforced in Redis-backed queue at dispatch time).
    # Safe default for unknown concurrency: at most 1 high-mode job running globally.
    max_high_running_global: int = Field(default=1, alias="MAX_HIGH_RUNNING_GLOBAL")

    # runtime model manager / allocator
    prewarm_whisper: str = Field(default="", alias="PREWARM_WHISPER")  # comma-separated models
    prewarm_tts: str = Field(default="", alias="PREWARM_TTS")  # comma-separated models
    # If enabled, the UI/API may trigger background model downloads/prewarm.
    # OFF by default to avoid accidental internet dependence.
    enable_model_downloads: bool = Field(default=False, alias="ENABLE_MODEL_DOWNLOADS")
    model_cache_max: int = Field(default=3, alias="MODEL_CACHE_MAX")
    gpu_util_max: float = Field(default=0.85, alias="GPU_UTIL_MAX")
    gpu_mem_max_ratio: float = Field(default=0.90, alias="GPU_MEM_MAX_RATIO")

    # --- job submission idempotency ---
    idempotency_ttl_sec: int = Field(default=86400, alias="IDEMPOTENCY_TTL_SEC")

    # --- Level 2 queue (Redis) ---
    # auto: use Redis if configured+healthy, else fallback to local queue
    # redis: require Redis (falls back only if unreachable at runtime)
    # fallback: force local queue
    queue_mode: str = Field(default="auto", alias="QUEUE_MODE")  # auto|redis|fallback
    # Redis key prefix for queue/locks/counters (no secrets)
    redis_queue_prefix: str = Field(default="dp", alias="REDIS_QUEUE_PREFIX")
    # Per-job lock lease (ms). Must be refreshed while a job runs.
    redis_lock_ttl_ms: int = Field(default=300_000, alias="REDIS_LOCK_TTL_MS")  # 5 minutes
    redis_lock_refresh_ms: int = Field(default=20_000, alias="REDIS_LOCK_REFRESH_MS")  # 20 seconds
    # Queue delivery attempts (primarily for crash re-delivery / dispatch failures)
    redis_queue_max_attempts: int = Field(default=8, alias="REDIS_QUEUE_MAX_ATTEMPTS")
    redis_queue_backoff_ms: int = Field(default=750, alias="REDIS_QUEUE_BACKOFF_MS")
    redis_queue_backoff_cap_ms: int = Field(default=30_000, alias="REDIS_QUEUE_BACKOFF_CAP_MS")
    # Cancel flag TTL (ms): how long to keep cancel markers (helps late consumers)
    redis_cancel_ttl_ms: int = Field(default=24 * 3600_000, alias="REDIS_CANCEL_TTL_MS")
    # Active set TTL (ms): keep per-user active job sets bounded
    redis_active_set_ttl_ms: int = Field(default=6 * 3600_000, alias="REDIS_ACTIVE_SET_TTL_MS")

    # --- job limits/watchdogs ---
    max_video_min: int = Field(default=120, alias="MAX_VIDEO_MIN")
    # Optional: reject videos above these caps (0 disables)
    max_video_width: int = Field(default=0, alias="MAX_VIDEO_WIDTH")
    max_video_height: int = Field(default=0, alias="MAX_VIDEO_HEIGHT")
    max_video_pixels: int = Field(default=0, alias="MAX_VIDEO_PIXELS")
    max_upload_mb: int = Field(default=2048, alias="MAX_UPLOAD_MB")
    max_concurrent_per_user: int = Field(default=1, alias="MAX_CONCURRENT")
    daily_processing_minutes: int = Field(default=240, alias="DAILY_PROCESSING_MINUTES")

    # --- submission policy ---
    # Safe defaults: allow 1 running job per user, small queue, and restrict high mode.
    max_active_jobs_per_user: int = Field(default=1, alias="DUBBING_MAX_ACTIVE_JOBS_PER_USER")
    max_queued_jobs_per_user: int = Field(default=5, alias="DUBBING_MAX_QUEUED_JOBS_PER_USER")
    daily_job_cap: int = Field(default=0, alias="DUBBING_DAILY_JOB_CAP")  # 0 disables
    high_mode_admin_only: bool = Field(default=True, alias="DUBBING_HIGH_MODE_ADMIN_ONLY")

    watchdog_audio_s: int = Field(default=10 * 60, alias="WATCHDOG_AUDIO_S")
    watchdog_diarize_s: int = Field(default=20 * 60, alias="WATCHDOG_DIARIZE_S")
    watchdog_whisper_s: int = Field(default=45 * 60, alias="WATCHDOG_WHISPER_S")
    watchdog_translate_s: int = Field(default=10 * 60, alias="WATCHDOG_TRANSLATE_S")
    watchdog_tts_s: int = Field(default=30 * 60, alias="WATCHDOG_TTS_S")
    watchdog_mix_s: int = Field(default=20 * 60, alias="WATCHDOG_MIX_S")
    watchdog_mux_s: int = Field(default=20 * 60, alias="WATCHDOG_MUX_S")
    watchdog_export_s: int = Field(default=20 * 60, alias="WATCHDOG_EXPORT_S")
    # Optional: memory cap for watchdog child processes (0 disables)
    watchdog_child_max_mem_mb: int = Field(default=0, alias="WATCHDOG_CHILD_MAX_MEM_MB")

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
    otel_service_name: str = Field(default="dubbing_pipeline", alias="OTEL_SERVICE_NAME")

    # --- WebRTC defaults (TURN creds live in secrets) ---
    webrtc_stun: str = Field(default="stun:stun.l.google.com:19302", alias="WEBRTC_STUN")
    webrtc_idle_timeout_s: int = Field(default=300, alias="WEBRTC_IDLE_TIMEOUT_S")
    webrtc_max_pcs_per_ip: int = Field(default=2, alias="WEBRTC_MAX_PCS_PER_IP")
    # Do NOT expose TURN credentials to clients unless explicitly enabled.
    webrtc_expose_turn_credentials: bool = Field(default=False, alias="WEBRTC_EXPOSE_TURN_CREDENTIALS")

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in (self.cors_origins or "").split(",") if o.strip()]
