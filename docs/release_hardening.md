## Release hardening report

Repository: `tommy2202/Dubbing-Pipeline` (workspace snapshot)

### Repo entrypoints

- **CLI (v2)**: `anime-v2` → `src/anime_v2/cli.py:cli` (also supports subcommands: `review`, `qa`, etc.)
- **CLI (v1 legacy)**: `anime-v1` → `src/anime_v1/cli.py:cli`
- **Web server (FastAPI + UI)**: `anime-v2-web` → `src/anime_v2/web/run.py:main` (runs `anime_v2.server:app`)
- **Server app module**: `src/anime_v2/server.py` (`app = FastAPI(...)`)
- **Batch worker**: `src/anime_v2/batch_worker.py:main`
- **Ops utilities**:
  - `src/anime_v2/ops/backup.py:main`
  - `src/anime_v2/ops/retention.py:main`
- **Verification scripts** (must run offline / without real media): `scripts/verify_*.py`, `scripts/smoke_import_all.py`, `scripts/smoke_run.py`

### Pipeline stage map (v2)

This is the canonical “happy path” used by `src/anime_v2/cli.py` and `src/anime_v2/jobs/queue.py`.

- **Settings / config**
  - `config/settings.py:get_settings`, `config/settings.py:SETTINGS`
  - v2 wrapper: `src/anime_v2/config.py:get_settings`
- **Stage: audio extract**
  - `src/anime_v2/stages/audio_extractor.py:run` / `extract`
  - uses: `src/anime_v2/utils/ffmpeg_safe.py:extract_audio_mono_16k`
- **Stage: diarization (optional)**
  - `src/anime_v2/stages/diarization.py:diarize` (invoked via `cli.py`/`jobs/queue.py`)
  - optional smoothing: `src/anime_v2/diarization/smoothing.py:detect_scenes_audio`, `smooth_speakers_in_scenes`
- **Stage: ASR (Whisper)**
  - `src/anime_v2/stages/transcription.py:transcribe`
- **Stage: MT translation (optional)**
  - `src/anime_v2/stages/translation.py:translate_segments` (+ `TranslationConfig`)
  - Tier‑Next/E additions (post-translate transforms):
    - `src/anime_v2/text/style_guide.py:apply_style_guide_to_segments`
    - `src/anime_v2/text/pg_filter.py:apply_pg_filter_to_segments`
- **Stage: timing fit + pacing (optional)**
  - `src/anime_v2/timing/fit_text.py:*`
  - `src/anime_v2/timing/pacing.py:*`
- **Stage: TTS**
  - `src/anime_v2/stages/tts.py:run`
  - TTS provider: `src/anime_v2/stages/tts_engine.py`
  - Director mode: `src/anime_v2/expressive/director.py:plan_for_segment`
  - Music suppression: `src/anime_v2/audio/music_detect.py:should_suppress_segment`
- **Stage: enhanced mixing (optional)**
  - Separation: `src/anime_v2/audio/separation.py:separate_dialogue` (Demucs optional)
  - Mix: `src/anime_v2/audio/mix.py:mix_dubbed_audio`
  - Music preservation bed: `src/anime_v2/audio/music_detect.py:build_music_preserving_bed`
- **Stage: export (containers)**
  - `src/anime_v2/stages/export.py:export_mkv`, `export_mp4`, `export_hls`
  - legacy/simple mux: `src/anime_v2/stages/mkv_export.py:mux`
  - **multitrack MKV**: `src/anime_v2/stages/export.py:export_mkv_multitrack`
- **Stage: QA scoring (optional)**
  - `src/anime_v2/qa/scoring.py:score_job`
  - CLI: `src/anime_v2/qa/cli.py`
- **Tier‑2 Review loop**
  - CLI: `src/anime_v2/review/cli.py`
  - Ops: `src/anime_v2/review/ops.py`
  - State model: `src/anime_v2/review/state.py`
- **Tier‑2 Voice memory**
  - `src/anime_v2/voice_memory/*` (store, embedding providers, mapping)
- **Tier‑3 Streaming**
  - Chunking: `src/anime_v2/streaming/chunker.py`
  - Orchestration: `src/anime_v2/streaming/runner.py`
- **Tier‑3 Lip-sync plugin**
  - Interface: `src/anime_v2/plugins/lipsync/base.py`
  - Wav2Lip: `src/anime_v2/plugins/lipsync/wav2lip_plugin.py`

### Risks (high/medium/low) with concrete issues

#### High risk

- **ASR fallback stub is broken (never triggers)**
  - File: `src/anime_v2/stages/transcription.py`
  - Problem: a `try: pass` block is used where a real import/availability check should exist. This makes the “Whisper not installed” fallback dead code and can produce confusing failures.
  - Outcome risk: hard crash or non-actionable error when Whisper isn’t installed; violates “explicit failure modes”.
- **Dependency pin conflicts between `pyproject.toml` and `docker/constraints.txt`**
  - `pyproject.toml` declares wide ranges (e.g., `numpy>=1.24` in extras), while `docker/constraints.txt` pins `numpy==1.22.0` with a comment tied to Py≤3.10.
  - Outcome risk: developers running non-Docker installs can easily end up with incompatible resolver results; Docker uses Ubuntu 22.04 Python 3.10 so it happens to work, but the mismatch is confusing and brittle.
- **Multiple time-stretch implementations (risk of diverging behavior)**
  - Files: `src/anime_v2/stages/align.py:retime_tts` (librosa path) vs `src/anime_v2/timing/pacing.py` (ffmpeg atempo)
  - Outcome risk: two “canonical” retime behaviors causing drift and hard-to-debug differences between code paths.

#### Medium risk

- **Legacy text style/profanity logic coexists with Tier‑Next style guide + PG filter**
  - File: `src/anime_v2/stages/translation.py` (`_apply_style`, `_read_style`, etc.)
  - Outcome risk: duplicate transformations and ordering ambiguity; can conflict with deterministic Tier‑Next E/C systems.
- **Non-canonical subprocess usage**
  - File: `src/anime_v2/utils/hashio.py` uses `subprocess.Popen(...)` directly (not routed through the canonical runner). This is probably intentional (stream hashing), but should be made consistent (timeouts, stderr capture, error context).

#### Low risk

- **Metadata/doc drift**
  - README claims features/flags that may not exactly match `--help` output in `anime-v2`. Needs a command-by-command verification sweep.
- **Output naming variations**
  - CLI uses `Output/<job>/dub.mkv`; queue uses `<stem>.dub.mkv` in job root. Not wrong, but must be documented clearly.

### Fix plan checklist (ordered, with expected outcomes)

1) **Phase 1: correctness/import safety**
   - Fix the ASR fallback logic in `transcription.py` so missing Whisper yields a clear, non-destructive fallback artifact (or a clear error if ASR is required).
   - Run import smoke across all entrypoints and major feature modules; eliminate circular imports by lazy-loading heavy deps.
   - Expected: `python3 scripts/smoke_import_all.py` always exits 0 on a minimal environment.

2) **Phase 2: dependency/version hardening**
   - Define the canonical dependency sources:
     - `pyproject.toml` for normal installs + extras
     - `docker/constraints.txt` only for Docker image pinning
   - Reconcile documented Python versions and ensure constraints are not misleading.
   - Add `scripts/verify_env.py` that reports required vs optional deps and feature availability.
   - Expected: new users can run `verify_env.py` and know exactly what’s enabled/disabled.

3) **Phase 3: remove placeholders/obsolete paths**
   - Repo-wide scan for TODO/FIXME/WIP/stubs and eliminate or route into the canonical implementations.
   - Consolidate time-stretch/pacing into a single canonical module; keep legacy behavior by default.
   - Expected: “one source of truth per concern” with no stale imports.

4) **Phase 4–5: robustness + logging polish**
   - Introduce a minimal `JobContext` and stage manifests without breaking current checkpoint behavior.
   - Ensure ffmpeg stderr capture is persisted per-job and per-command.
   - Add CLI flags for log level/json/debug-dump in a backwards-compatible way.
   - Expected: failures are explicit and non-destructive, with actionable logs under `Output/<job>/logs/`.

5) **Phase 6: polish gate**
   - Add `scripts/polish_gate.py` that runs smoke/env/feature synthetic checks and fails on stubs/dup modules (allowlist only).
   - Expected: one command provides release confidence and points to detailed logs.

