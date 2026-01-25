# Setup Wizard / Doctor Plan (Repo Scan)

This document captures the repo scan and proposes how to implement a
professional "Setup Wizard / Doctor" (host + container) without duplicating
existing helpers.

---

## 0) Repo scan highlights (entrypoints + directories)

### CLI entrypoints (existing)
- `pyproject.toml` -> `[project.scripts]`
  - `dubbing-pipeline` / `dub` -> `dubbing_pipeline.cli:cli`
  - `dubbing-web` / `dub-web` -> `dubbing_pipeline.web.run:main`
  - `dub-legacy` -> `dubbing_pipeline_legacy.cli:cli`
- Root shim for `uvicorn main:app`: `main.py` (re-exports `dubbing_pipeline.server:app`)
- Additional Click subcommands:
  - `src/dubbing_pipeline/review/cli.py`
  - `src/dubbing_pipeline/overrides/cli.py`
  - `src/dubbing_pipeline/qa/cli.py`
  - `src/dubbing_pipeline/character/cli.py`
  - `src/dubbing_pipeline/plugins/lipsync/cli.py`
  - `src/dubbing_pipeline/voice_memory/cli.py`
- Legacy CLI: `src/dubbing_pipeline_legacy/cli.py`

### Logs / docs / scripts directories
- Docs: `docs/` (large set of setup + ops guides)
- Scripts: `scripts/` (verify_*, smoke_*, gates, diagnostics, downloads)
- Runtime dirs (git-kept empty): `Input/`, `Output/` (see README)
- Log directories:
  - app logs: `log_dir` default `./logs` (see `config/public_config.py`)
  - per-job logs: `Output/<job>/logs` (see `src/dubbing_pipeline/utils/job_logs.py`)

### Smoke tests + verification scripts
- Smoke tests (pytest):
  - `tests/test_smoke.py`
  - `tests/test_smoke_fresh_machine.py`
  - helper: `tests/_helpers/smoke_fresh_machine.py`
- Smoke scripts:
  - `scripts/smoke_import_all.py`
  - `scripts/smoke_run.py`
  - `scripts/smoke_fresh_machine.py`
  - `scripts/smoke_segment_pacing.py`
- Verify scripts (subset; there are many in `scripts/verify_*.py`):
  - `verify_env.py` (dependency + tool checks)
  - `verify_readiness.py` (API readiness keys)
  - `verify_model_manager.py` (runtime models endpoint)
  - `verify_modes_contract.py` (tests for mode logic)
  - `verify_runtime.py`, `verify_queue_*`, `verify_retention.py`,
    `verify_log_redaction.py`, `verify_lipsync_plugin.py`, etc.

---

## 1) Where to add the `doctor` CLI subcommand

**Exact file + function:**
- File: `src/dubbing_pipeline/cli.py`
  - Add an import near the bottom (with the other CLI imports):
    `from dubbing_pipeline.doctor.cli import doctor`
  - Register the subcommand with the Click group:
    `cli.add_command(doctor)`

**Command implementation location:**
- Create `src/dubbing_pipeline/doctor/cli.py` with a Click command function:
  - `@click.command(name="doctor")`
  - `def doctor(...): ...`

This is consistent with how other subcommands are organized and registered in
`cli.py` (see the block that adds `review`, `qa`, `overrides`, etc.).

---

## 2) Existing helpers to reuse (avoid duplication)

### Config loaders / settings
- `config/public_config.py` (paths, model settings, log settings)
- `config/secret_config.py` (secrets)
- `config/settings.py` (`get_settings`, `get_safe_config_report`)
- `src/dubbing_pipeline/config.py` (compat shim)
- `scripts/show_config.py` (effective config report)

### Logging utilities
- `src/dubbing_pipeline/utils/log.py` (structlog config, redaction, log_dir)
- `src/dubbing_pipeline/utils/job_logs.py` (per-job log paths + artifacts)

### Subprocess and ffmpeg helpers
- `src/dubbing_pipeline/utils/ffmpeg_safe.py` (safe ffmpeg/ffprobe runner)
- `src/dubbing_pipeline/utils/ffmpeg.py` (canonical ffmpeg helpers)
- `src/dubbing_pipeline/runtime/lifecycle.py` (`_run_version`, `_startup_self_check`)
- `scripts/e2e_ffmpeg_cases.py` (ffmpeg edge cases)

### Storage/path helpers
- `src/dubbing_pipeline/utils/paths.py` (`default_paths`, Output/Input/logs layout)
- `src/dubbing_pipeline/ops/storage.py` (`ensure_free_space`, retention hooks)

### Network/egress guard
- `src/dubbing_pipeline/utils/net.py` (`install_egress_policy`, `egress_guard`)

### Model management + downloads
- `src/dubbing_pipeline/runtime/model_manager.py` (Whisper + TTS caching)
- `src/dubbing_pipeline/api/routes_runtime.py` (`/api/runtime/models`, prewarm)
- `src/dubbing_pipeline/api/routes_system.py` (readiness checks + cache detection)
- `scripts/download_models.py` (prefetch Whisper/HF/Wav2Lip weights)
- `src/dubbing_pipeline/plugins/lipsync/wav2lip_plugin.py` (Wav2Lip paths)

---

## 3) Proposed file list to create/modify (minimal changes)

**New files (doctor implementation):**
- `src/dubbing_pipeline/doctor/__init__.py`
- `src/dubbing_pipeline/doctor/cli.py` (Click entrypoint)
- `src/dubbing_pipeline/doctor/host.py` (host checks)
- `src/dubbing_pipeline/doctor/container.py` (container checks)
- `src/dubbing_pipeline/doctor/report.py` or `checks.py` (shared data model)

**Existing files to modify:**
- `src/dubbing_pipeline/cli.py` (import + `cli.add_command(doctor)`)

**Docs updates (later):**
- `README.md` (add "doctor" to quickstart)
- `docs/SETUP.md` / `docs/TROUBLESHOOTING.md` (doctor usage + examples)

---

## 4) Checklist (PASS/WARN/FAIL)

Below are concrete checks aligned with existing code paths.

### Host doctor (local / bare-metal)

**PASS**
- Python >= 3.10 (matches `pyproject.toml`)
- `ffmpeg` + `ffprobe` available (use `_run_version` from `runtime/lifecycle.py`)
- `get_settings()` loads, `get_safe_config_report()` returns
- Output/Input/logs directories exist and are writable
  - Use `utils.paths.default_paths()` + `_ensure_writable_dir` from `runtime/lifecycle.py`
- Disk space above `MIN_FREE_GB` if configured (`ops/storage.ensure_free_space`)

**WARN**
- Optional deps missing (from `scripts/verify_env.py`):
  - whisper / torch / TTS / demucs / pyannote / aeneas / librosa / etc
- GPU not available (from `modes.HardwareCaps.detect`)
- Egress disabled and models not cached (see readiness checks)
- Redis configured but unreachable (queue can fallback)

**FAIL**
- Required deps missing (ffmpeg/ffprobe, core Python deps)
- Settings load fails (invalid env / secrets in strict mode)
- Output/Input/logs dir not writable

### Container doctor (quick)

**PASS**
- `APP_ROOT` resolved (defaults to `/app` if present; see `PublicConfig._default_app_root`)
- Container has `ffmpeg` + `ffprobe`
- Output/Input/logs paths are writable
- Import smoke: `scripts/smoke_import_all.py`

**WARN**
- `/models` mount missing when `MODELS_DIR=/models`
- OFFLINE_MODE with missing cache (expect first-run download failures)

**FAIL**
- ffmpeg/ffprobe missing in container
- config load fails
- required directories not writable

### Container doctor (full)

**PASS**
- All "quick" checks
- `/api/system/readiness` returns items list (see `routes_system.py`)
- `/api/runtime/models` returns path + disk info (see `routes_runtime.py`)
- Optional: `scripts/verify_readiness.py`, `verify_model_manager.py`
- Optional: `scripts/smoke_run.py` with `SMOKE_RUN_PIPELINE=1`

**WARN**
- Readiness items are `Disabled` for optional features
  (GPU, demucs, pyannote, Wav2Lip, etc.)
- `ENABLE_MODEL_DOWNLOADS=0` (prewarm disabled)
- `ALLOW_EGRESS=0` + missing model caches

**FAIL**
- Readiness endpoint unreachable or returns error
- Required readiness items are `Missing` for baseline pipeline

---

## 5) Model/weights verification for HIGH mode (based on existing code)

**Source of truth for mode behavior**
- `src/dubbing_pipeline/modes.py`:
  - `resolve_effective_settings(mode="high", ...)`
  - `asr_model = "large-v3"` if GPU is available, else `medium`
  - `separation = demucs` in high mode
  - `voice_memory`, `speaker_smoothing`, `voice_mode=clone` enabled in high

**Weights / cache checks already implemented**
- `src/dubbing_pipeline/api/routes_system.py`:
  - `_whisper_model_cached()` + `_whisper_cache_dirs()` for Whisper weights
  - `_hf_model_cached()` for HF models (uses `transformers_cache` / `hf_home`)
  - readiness items: `whisper_models`, `translation_offline`, `xtts`, `diarization`,
    `separation`, `lipsync`, `storage_backend`, `retention`

**Model locations and config**
- `config/public_config.py`:
  - `models_dir` (default `/models`)
  - `whisper_model`, `tts_model`, `tts_basic_model`, `translation_model`
  - caches: `hf_home`, `torch_home`, `tts_home`, `transformers_cache`
  - Wav2Lip config: `wav2lip_dir`, `wav2lip_checkpoint`
- `src/dubbing_pipeline/plugins/lipsync/wav2lip_plugin.py`:
  - resolves Wav2Lip repo + checkpoint paths

**Download helpers**
- `scripts/download_models.py`:
  - Whisper `large`
  - HF models (m2m100, Marian)
  - Wav2Lip weights + repo clone
- `src/dubbing_pipeline/runtime/model_manager.py` + `/api/runtime/models/prewarm`

**Doctor verification steps for HIGH mode**
1. Call `resolve_effective_settings(mode="high")` to decide ASR model.
2. Validate Whisper cache for the selected ASR model using readiness logic.
3. Confirm TTS model availability via `ModelManager` / readiness `xtts`.
4. If high-mode separation is enabled, verify `demucs` import (readiness `separation`).
5. If diarization is in use, verify `pyannote.audio` or fallback path (readiness `diarization`).
6. If Wav2Lip is enabled, resolve repo + checkpoint via `Wav2LipPlugin._resolve_paths`.
7. Respect `OFFLINE_MODE` / `ALLOW_EGRESS` (warn or fail if caches are missing).

---

## 6) Golden path setup steps (README additions)

Based on current docs (`README.md`, `docs/CLEAN_SETUP_GUIDE.txt`,
`docs/GOLDEN_PATH_TAILSCALE.md`, `docs/SETUP.md`):

1. **Install prerequisites**
   - Python 3.10+, `ffmpeg`, `ffprobe`, Git
2. **Install package**
   - `python3 -m pip install -e .`
3. **Set core env**
   - `ADMIN_USERNAME`, `ADMIN_PASSWORD`
   - `ACCESS_MODE` (tailscale or local)
   - `HOST`, `PORT`, `DUBBING_OUTPUT_DIR`, `DUBBING_LOG_DIR`
4. **Offline vs online**
   - If offline: set `OFFLINE_MODE=1`, pre-download models:
     `python3 scripts/download_models.py`
5. **Run doctor**
   - `dubbing-pipeline doctor --host` (and container variants)
6. **Verify wiring**
   - `python3 scripts/verify_env.py`
   - `python3 scripts/verify_readiness.py`
   - `python3 scripts/smoke_import_all.py`
7. **Start server**
   - `dubbing-web` (local) or `./scripts/run_prod.sh` (tailscale path)
8. **First job**
   - `dubbing-pipeline Input/Test.mp4 --mode low --device cpu`

---

## 7) Mode definitions + model path references (explicit)

- Modes defined here:
  - `src/dubbing_pipeline/modes.py` (`ModeName`, `resolve_effective_settings`)
  - `src/dubbing_pipeline/cli.py` (CLI `--mode` choice)
  - `src/dubbing_pipeline/web/routes/jobs_submit.py` + templates
  - Legacy: `src/dubbing_pipeline_legacy/cli.py` (`_resolve_defaults`)
- Model paths / requirements:
  - `config/public_config.py` (all paths + model IDs)
  - `scripts/download_models.py` (what gets prefetched)
  - `src/dubbing_pipeline/plugins/lipsync/wav2lip_plugin.py` (Wav2Lip paths)
  - `src/dubbing_pipeline/api/routes_system.py` (cache detection)

