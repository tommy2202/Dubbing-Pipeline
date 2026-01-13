## Repository cleanup plan (repo hygiene / packaging / safety)

Constraints honored:
- **No pipeline logic refactors.**
- Focus on **repo hygiene**, **packaging boundaries**, and **safety** (secrets/PII/copyrighted media).
- This document started as a plan; see **Implementation status** for what was actually changed in-repo.

## Executive summary

This repository currently has multiple categories of **tracked runtime/build artifacts** that should not be in source control and must not be shipped in release zips:

- **Python bytecode caches** (`__pycache__/`, `*.pyc`) are committed (including under `src/` and `tests/`).
- **Setuptools build output** under `build/lib/**` is committed (duplicated source tree).
- **Runtime scratch output** under multiple `_tmp_*` directories is committed, including:
  - audio (`*.wav`)
  - sqlite dbs (`auth.db`, `jobs.db`)
  - intermediate JSON/JSONL/MD reports
- **Backups** are committed (`backups/*.zip` and manifest).
- **Runtime `Input/` content** is committed (`Input/Test.mp4`).
- Additional “ship risk” assets are committed (generated reports under `data/reports/**`, and `voices/embeddings/Speaker1.npy`).

Primary risks:
- **Security/privacy**: `*.db` and backups can contain tokens, user identifiers, job metadata, and other sensitive content.
- **Noise & bloat**: build trees and caches create large diffs, merge conflicts, and inflate release artifacts.
- **Licensing/copyright**: committed media files in runtime dirs (`Input/*.mp4`, `_tmp_*/**/*.wav`) are risky to redistribute.

## Implementation status (applied in-repo)

The following repo hygiene fixes were applied with minimal impact to working code:

- **`.gitignore`**:
  - Added/confirmed ignores for `__pycache__/`, `*.pyc`, `*.pyo`, `build/`, `dist/`, `*.egg-info/`, `Output/**`, `Input/**`, `backups/`, `logs/`, `*.log`, `_tmp*/`, `_tmp_*/`, `tmp/`, and `*.db`.
  - Added exceptions so placeholders remain tracked: `!Input/.gitkeep` and `!Output/.gitkeep`.

- **Placeholders**:
  - Added `Input/.gitkeep` and `Output/.gitkeep`.

- **Cleanup scripts**:
  - Added `scripts/cleanup_git_artifacts.sh` and `scripts/cleanup_git_artifacts.ps1`.
  - Both scripts default to **dry-run**, list what will be untracked, require confirmation unless `--yes` / `-Yes`, run `git rm -r --cached` only on **tracked** artifact paths, then re-add `.gitkeep` files (forced add).

- **Artifacts untracked from git (kept on disk)**:
  - Untracked **296** tracked artifact paths matching the patterns above, including:
    - `build/lib/**`
    - committed `__pycache__/**` and `*.pyc`
    - `_tmp_*` scratch outputs (including `*.db`, `*.wav`, and intermediate reports)
    - `backups/backup-20260101-0254.*`
    - `Input/Test.mp4`

- **Repo-internal junk deleted from disk (explicitly allowed)**:
  - Deleted `build/` and `__pycache__/` directories from the working tree after untracking (to reduce local noise). No runtime/user media or `_tmp_*` content was deleted from disk by the cleanup.

Items intentionally **not changed yet** (need a separate decision/policy because they may be fixtures):
- `data/reports/**`
- `voices/embeddings/Speaker1.npy`

## Inventory: tracked artifacts discovered (exact paths)

### A) Python runtime caches / bytecode (should never be tracked)

**Why unsafe/noisy**
- Not source; machine-specific; causes meaningless diffs.
- Can contain paths/usernames in metadata (minor privacy leak).
- Signals accidental execution artifacts in repo.

**Recommended disposition**
- **Delete from disk** (generated) and **untrack from git**.
- Keep excluded from **both** git and release zips.

Tracked paths:

```text
__pycache__/main.cpython-312.pyc
src/anime_v1/__pycache__/__init__.cpython-310.pyc
src/anime_v1/__pycache__/cli.cpython-310.pyc
src/anime_v1/stages/__pycache__/__init__.cpython-310.pyc
src/anime_v1/stages/__pycache__/audio_extractor.cpython-310.pyc
src/anime_v1/stages/__pycache__/diarisation.cpython-310.pyc
src/anime_v1/stages/__pycache__/mkv_export.cpython-310.pyc
src/anime_v1/stages/__pycache__/transcription.cpython-310.pyc
src/anime_v1/stages/__pycache__/tts.cpython-310.pyc
src/anime_v1/utils/__pycache__/__init__.cpython-310.pyc
src/anime_v1/utils/__pycache__/checkpoints.cpython-310.pyc
src/anime_v1/utils/__pycache__/log.cpython-310.pyc
src/anime_v2/api/__pycache__/__init__.cpython-312.pyc
src/anime_v2/api/__pycache__/deps.cpython-312.pyc
src/anime_v2/api/__pycache__/middleware.cpython-312.pyc
src/anime_v2/api/__pycache__/models.cpython-312.pyc
src/anime_v2/api/__pycache__/routes_audit.cpython-312.pyc
src/anime_v2/api/__pycache__/routes_auth.cpython-312.pyc
src/anime_v2/api/__pycache__/routes_jobs.cpython-312.pyc
src/anime_v2/api/__pycache__/routes_keys.cpython-312.pyc
src/anime_v2/api/__pycache__/routes_runtime.cpython-312.pyc
src/anime_v2/api/__pycache__/routes_settings.cpython-312.pyc
src/anime_v2/api/__pycache__/security.cpython-312.pyc
src/anime_v2/cache/__pycache__/__init__.cpython-312.pyc
src/anime_v2/cache/__pycache__/store.cpython-312.pyc
src/anime_v2/gates/__pycache__/license.cpython-312.pyc
src/anime_v2/ops/__pycache__/__init__.cpython-312.pyc
src/anime_v2/ops/__pycache__/audit.cpython-312.pyc
src/anime_v2/ops/__pycache__/backup.cpython-312.pyc
src/anime_v2/ops/__pycache__/metrics.cpython-312.pyc
src/anime_v2/ops/__pycache__/retention.cpython-312.pyc
src/anime_v2/ops/__pycache__/storage.cpython-312.pyc
src/anime_v2/runtime/__pycache__/__init__.cpython-312.pyc
src/anime_v2/runtime/__pycache__/device_allocator.cpython-312.pyc
src/anime_v2/runtime/__pycache__/lifecycle.cpython-312.pyc
src/anime_v2/runtime/__pycache__/model_manager.cpython-312.pyc
src/anime_v2/runtime/__pycache__/scheduler.cpython-312.pyc
tests/__pycache__/test_artifacts_qr.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_artifacts_qr.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_audit_recent.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_audit_recent.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_basic.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_basic.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_basic.cpython-312.pyc
tests/__pycache__/test_char_store_encryption.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_char_store_encryption.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_checkpoint.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_checkpoint.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_dashboard_api.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_dashboard_api.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_diar.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_diar.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_diar.cpython-312.pyc
tests/__pycache__/test_draining.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_draining.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_export.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_export.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_export.cpython-312.pyc
tests/__pycache__/test_idempotency.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_idempotency.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_job_detail_api.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_job_detail_api.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_job_limits.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_job_limits.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_model_manager.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_model_manager.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_pipeline_metrics.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_pipeline_metrics.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_presets_projects_batch.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_presets_projects_batch.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_rbac_csrf_ui.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_rbac_csrf_ui.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_retry_circuit.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_retry_circuit.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_scheduler.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_scheduler.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_settings_api.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_settings_api.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_smoke.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_smoke.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_storage.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_storage.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_transcript_editing.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_transcript_editing.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_translate.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_translate.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_translate.cpython-312.pyc
tests/__pycache__/test_tts.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_tts.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_tts.cpython-312.pyc
tests/__pycache__/test_ui_pages.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_ui_pages.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_utils.cpython-312-pytest-8.3.3.pyc
tests/__pycache__/test_utils.cpython-312-pytest-9.0.2.pyc
tests/__pycache__/test_utils.cpython-312.pyc
tools/__pycache__/build_voice_db.cpython-312.pyc
```

### B) Build artifacts: setuptools `build/lib/**` (should never be tracked)

**Why unsafe/noisy**
- This is **generated output** from local packaging/build runs (duplicates `src/`).
- Bloats the repo and confuses packaging/import resolution.
- Can lead to “stale build tree” bugs if shipped accidentally.

**Recommended disposition**
- **Delete from disk** and **untrack from git**.
- Add `build/` (and `dist/`) to `.gitignore`.
- Ensure release zips are built from clean source (see allowlist below).

Tracked paths (build output):

```text
build/lib/anime_v1/__init__.py
build/lib/anime_v1/cli.py
build/lib/anime_v1/stages/__init__.py
build/lib/anime_v1/stages/audio_extractor.py
build/lib/anime_v1/stages/diarisation.py
build/lib/anime_v1/stages/downloader.py
build/lib/anime_v1/stages/lipsync.py
build/lib/anime_v1/stages/mkv_export.py
build/lib/anime_v1/stages/separation.py
build/lib/anime_v1/stages/transcription.py
build/lib/anime_v1/stages/translation.py
build/lib/anime_v1/stages/tts.py
build/lib/anime_v1/utils/__init__.py
build/lib/anime_v1/utils/checkpoints.py
build/lib/anime_v1/utils/log.py
build/lib/anime_v2/__init__.py
build/lib/anime_v2/api/__init__.py
build/lib/anime_v2/api/auth/__init__.py
build/lib/anime_v2/api/auth/refresh_tokens.py
build/lib/anime_v2/api/deps.py
build/lib/anime_v2/api/middleware.py
build/lib/anime_v2/api/models.py
build/lib/anime_v2/api/remote_access.py
build/lib/anime_v2/api/routes_audit.py
build/lib/anime_v2/api/routes_auth.py
build/lib/anime_v2/api/routes_jobs.py
build/lib/anime_v2/api/routes_keys.py
build/lib/anime_v2/api/routes_runtime.py
build/lib/anime_v2/api/routes_settings.py
build/lib/anime_v2/api/security.py
build/lib/anime_v2/audio/__init__.py
build/lib/anime_v2/audio/mix.py
build/lib/anime_v2/audio/music_detect.py
build/lib/anime_v2/audio/separation.py
build/lib/anime_v2/audio/tracks.py
build/lib/anime_v2/batch_worker.py
build/lib/anime_v2/cache/__init__.py
build/lib/anime_v2/cache/store.py
build/lib/anime_v2/character/__init__.py
build/lib/anime_v2/character/cli.py
build/lib/anime_v2/cli.py
build/lib/anime_v2/config.py
build/lib/anime_v2/diarization/__init__.py
build/lib/anime_v2/diarization/smoothing.py
build/lib/anime_v2/expressive/__init__.py
build/lib/anime_v2/expressive/director.py
build/lib/anime_v2/expressive/policy.py
build/lib/anime_v2/expressive/prosody.py
build/lib/anime_v2/gates/license.py
build/lib/anime_v2/jobs/__init__.py
build/lib/anime_v2/jobs/checkpoint.py
build/lib/anime_v2/jobs/context.py
build/lib/anime_v2/jobs/limits.py
build/lib/anime_v2/jobs/manifests.py
build/lib/anime_v2/jobs/models.py
build/lib/anime_v2/jobs/queue.py
build/lib/anime_v2/jobs/store.py
build/lib/anime_v2/jobs/watchdog.py
build/lib/anime_v2/modes.py
build/lib/anime_v2/notify/__init__.py
build/lib/anime_v2/notify/base.py
build/lib/anime_v2/notify/ntfy.py
build/lib/anime_v2/ops/__init__.py
build/lib/anime_v2/ops/audit.py
build/lib/anime_v2/ops/backup.py
build/lib/anime_v2/ops/metrics.py
build/lib/anime_v2/ops/retention.py
build/lib/anime_v2/ops/storage.py
build/lib/anime_v2/overrides/__init__.py
build/lib/anime_v2/overrides/cli.py
build/lib/anime_v2/plugins/__init__.py
build/lib/anime_v2/plugins/lipsync/__init__.py
build/lib/anime_v2/plugins/lipsync/base.py
build/lib/anime_v2/plugins/lipsync/cli.py
build/lib/anime_v2/plugins/lipsync/preview.py
build/lib/anime_v2/plugins/lipsync/registry.py
build/lib/anime_v2/plugins/lipsync/wav2lip_plugin.py
build/lib/anime_v2/projects/__init__.py
build/lib/anime_v2/projects/loader.py
build/lib/anime_v2/qa/__init__.py
build/lib/anime_v2/qa/cli.py
build/lib/anime_v2/qa/scoring.py
build/lib/anime_v2/realtime.py
build/lib/anime_v2/reports/__init__.py
build/lib/anime_v2/reports/drift.py
build/lib/anime_v2/review/__init__.py
build/lib/anime_v2/review/cli.py
build/lib/anime_v2/review/ops.py
build/lib/anime_v2/review/overrides.py
build/lib/anime_v2/review/state.py
build/lib/anime_v2/runtime/__init__.py
build/lib/anime_v2/runtime/device_allocator.py
build/lib/anime_v2/runtime/lifecycle.py
build/lib/anime_v2/runtime/model_manager.py
build/lib/anime_v2/runtime/scheduler.py
build/lib/anime_v2/security/__init__.py
build/lib/anime_v2/security/crypto.py
build/lib/anime_v2/security/field_crypto.py
build/lib/anime_v2/security/privacy.py
build/lib/anime_v2/server.py
build/lib/anime_v2/stages/__init__.py
build/lib/anime_v2/stages/align.py
build/lib/anime_v2/stages/audio_extractor.py
build/lib/anime_v2/stages/character_store.py
build/lib/anime_v2/stages/diarization.py
build/lib/anime_v2/stages/export.py
build/lib/anime_v2/stages/mixing.py
build/lib/anime_v2/stages/mkv_export.py
build/lib/anime_v2/stages/transcription.py
build/lib/anime_v2/stages/translate.py
build/lib/anime_v2/stages/translation.py
build/lib/anime_v2/stages/tts.py
build/lib/anime_v2/stages/tts_engine.py
build/lib/anime_v2/storage/__init__.py
build/lib/anime_v2/storage/retention.py
build/lib/anime_v2/streaming/__init__.py
build/lib/anime_v2/streaming/chunker.py
build/lib/anime_v2/streaming/context.py
build/lib/anime_v2/streaming/runner.py
build/lib/anime_v2/subs/__init__.py
build/lib/anime_v2/subs/formatting.py
build/lib/anime_v2/text/__init__.py
build/lib/anime_v2/text/pg_filter.py
build/lib/anime_v2/text/style_guide.py
build/lib/anime_v2/timing/__init__.py
build/lib/anime_v2/timing/fit_text.py
build/lib/anime_v2/timing/pacing.py
build/lib/anime_v2/timing/rewrite_provider.py
build/lib/anime_v2/utils/__init__.py
build/lib/anime_v2/utils/circuit.py
build/lib/anime_v2/utils/config.py
build/lib/anime_v2/utils/crypto.py
build/lib/anime_v2/utils/cues.py
build/lib/anime_v2/utils/embeds.py
build/lib/anime_v2/utils/ffmpeg.py
build/lib/anime_v2/utils/ffmpeg_safe.py
build/lib/anime_v2/utils/hashio.py
build/lib/anime_v2/utils/io.py
build/lib/anime_v2/utils/job_logs.py
build/lib/anime_v2/utils/log.py
build/lib/anime_v2/utils/net.py
build/lib/anime_v2/utils/paths.py
build/lib/anime_v2/utils/ratelimit.py
build/lib/anime_v2/utils/retry.py
build/lib/anime_v2/utils/security.py
build/lib/anime_v2/utils/subtitles.py
build/lib/anime_v2/utils/time.py
build/lib/anime_v2/utils/vad.py
build/lib/anime_v2/voice_memory/__init__.py
build/lib/anime_v2/voice_memory/audition.py
build/lib/anime_v2/voice_memory/cli.py
build/lib/anime_v2/voice_memory/embeddings.py
build/lib/anime_v2/voice_memory/store.py
build/lib/anime_v2/voice_memory/tools.py
build/lib/anime_v2/web/__init__.py
build/lib/anime_v2/web/app.py
build/lib/anime_v2/web/routes_jobs.py
build/lib/anime_v2/web/routes_ui.py
build/lib/anime_v2/web/routes_webrtc.py
build/lib/anime_v2/web/run.py
build/lib/config/__init__.py
build/lib/config/public_config.py
build/lib/config/secret_config.py
build/lib/config/settings.py
```

### C) Runtime scratch output: `_tmp_*` (should never be tracked)

**Why unsafe/noisy**
- These are clearly execution artifacts (audio, intermediate reports, and state).
- Can contain copyrighted audio, user prompts/outputs, and pipeline traces.
- Increases repo size and makes releases unsafe by default.

**Recommended disposition**
- **Delete from disk** and **untrack from git**.
- Keep excluded from release zips.

Tracked paths:

```text
_tmp_director/audio.wav
_tmp_director/expressive/director_plans.jsonl
_tmp_music_detect/bed.wav
_tmp_music_detect/bed2.wav
_tmp_music_detect/full.wav
_tmp_music_detect/music.wav
_tmp_music_detect/silence.wav
_tmp_music_detect/speech.wav
_tmp_music_detect/test_between.wav
_tmp_music_detect/test_between2.wav
_tmp_music_detect/test_if.wav
_tmp_ops/Output/auth.db
_tmp_ops2/Output/auth.db
_tmp_ops2/Output/jobs.db
_tmp_ops3/Output/auth.db
_tmp_ops3/Output/jobs.db
_tmp_pg_filter/pg_filter_report.json
_tmp_qa_job/analysis/music_regions.json
_tmp_qa_job/qa/segment_scores.jsonl
_tmp_qa_job/qa/summary.json
_tmp_qa_job/qa/top_issues.md
_tmp_qa_job/review/audio/1_v1.wav
_tmp_qa_job/review/state.json
_tmp_qa_job/translated.json
_tmp_qa_job/tts_clips/0001_B.wav
_tmp_qa_job/tts_clips/0002_A.wav
_tmp_qa_job/tts_clips/0003_B.wav
_tmp_qa_job/tts_manifest.json
_tmp_sched/Output/auth.db
_tmp_sched/Output/jobs.db
_tmp_speaker_smoothing/analysis/speaker_smoothing.json
_tmp_speaker_smoothing/audio.wav
_tmp_style_guide/style_guide.json
```

### D) SQLite DBs (especially `auth.db`) tracked inside runtime output

**Why unsafe/noisy**
- `auth.db` / `jobs.db` likely contain **credentials**, **session data**, **job history**, and other sensitive metadata.
- DB files are binary and can silently accumulate PII over time.

**Recommended disposition**
- **Delete from disk** and **untrack from git**.
- Keep excluded from release zips.
- Add CI gate forbidding `*.db` / `*.sqlite*` in tracked files (see below).

Tracked DB paths (subset, from `_tmp_*`):

```text
_tmp_ops/Output/auth.db
_tmp_ops2/Output/auth.db
_tmp_ops2/Output/jobs.db
_tmp_ops3/Output/auth.db
_tmp_ops3/Output/jobs.db
_tmp_sched/Output/auth.db
_tmp_sched/Output/jobs.db
```

### E) `Input/` contents tracked (runtime input directory)

**Why unsafe/noisy**
- `Input/` is explicitly a **runtime** folder (already ignored by `.gitignore`).
- Committing real media under runtime dirs is high risk (copyright/PII), and it encourages “it works on my machine” flows.

**Recommended disposition**
- **Untrack from git** and **delete from disk** (runtime input).
- If a sample is needed, keep it only under a dedicated sample/fixture directory and exclude from release zips.

Tracked paths:

```text
Input/Test.mp4
```

### F) Backups committed (`backups/`)

**Why unsafe/noisy**
- Backups commonly include **db files, configs, job artifacts, and potentially secrets**.
- Shipping a backup zip in a release is unsafe; committing it risks accidental redistribution and long-lived secret exposure.

**Recommended disposition**
- **Delete from disk** and **untrack from git**.
- Add `backups/` to `.gitignore`.
- If backups must exist, store them outside git (object storage, encrypted vault, etc.).

Tracked paths:

```text
backups/backup-20260101-0254.manifest.json
backups/backup-20260101-0254.zip
```

### G) Media artifacts tracked (wavs/mp4/zip)

**Why unsafe/noisy**
- Audio/video files are often copyrighted and frequently derived from user inputs.
- They bloat the repo and should not be included in release zips by default.

**Recommended disposition**
- Runtime media under `_tmp_*` and `Input/` should be **deleted + untracked**.
- `samples/sample.mp4` is a special case: it may be a legitimate test fixture, but should be **excluded from release zips**; consider Git LFS or downloading in CI.

Tracked paths:

```text
Input/Test.mp4
_tmp_director/audio.wav
_tmp_music_detect/bed.wav
_tmp_music_detect/bed2.wav
_tmp_music_detect/full.wav
_tmp_music_detect/music.wav
_tmp_music_detect/silence.wav
_tmp_music_detect/speech.wav
_tmp_music_detect/test_between.wav
_tmp_music_detect/test_between2.wav
_tmp_music_detect/test_if.wav
_tmp_qa_job/review/audio/1_v1.wav
_tmp_qa_job/tts_clips/0001_B.wav
_tmp_qa_job/tts_clips/0002_A.wav
_tmp_qa_job/tts_clips/0003_B.wav
_tmp_speaker_smoothing/audio.wav
backups/backup-20260101-0254.zip
samples/sample.mp4
```

### H) Generated reports/data currently committed

These may be intentional “example outputs”, but they are also consistent with generated runtime/report output.

**Why unsafe/noisy**
- Reports can contain traces of inputs and processing output, which can be sensitive.
- They become stale and are hard to keep consistent.

**Recommended disposition (choose one policy)**
- **Preferred**: treat as generated → **delete + untrack**, and generate on demand.
- **Alternative**: treat as test fixtures → move under `tests/fixtures/` (or similar) and keep excluded from release zips (fixtures rarely belong in runtime releases).

Tracked paths:

```text
data/reports/default/episodes/25fc400e824a2b87556c0afae8a341c6.json
data/reports/default/episodes/85d497fe5365207be8e4821502e99279.json
data/reports/default/episodes/a986b952c2c22562737a4706f0424dbc.json
data/reports/default/season_report.md
```

### I) Binary embedding / model-ish artifact tracked

**Why unsafe/noisy**
- Voice embeddings can be biometric-ish derived data and may be sensitive.
- Binary artifacts inflate releases and can’t be reviewed easily.

**Recommended disposition**
- **Do not ship** in release zips by default.
- Consider **delete + untrack** unless this is an intentional, licensed, non-sensitive fixture.

Tracked path:

```text
voices/embeddings/Speaker1.npy
```

## Action matrix (what to do with each category)

- **Python caches (`__pycache__/`, `*.pyc`)**
  - **Why**: generated, noisy, sometimes leaks env paths.
  - **Action**: delete + untrack; keep ignored; exclude from packaging.

- **Build output (`build/`, `dist/`, `*.egg-info/`)**
  - **Why**: generated; duplicates source; stale artifacts can shadow real code.
  - **Action**: delete + untrack; ignore in git; exclude from packaging.

- **Runtime dirs (`Input/`, `Output/`, `uploads/`, `outputs/`, `_tmp*/`)**
  - **Why**: runtime state and user content; high copyright/PII risk.
  - **Action**: delete + untrack anything currently committed there; keep ignored; exclude from packaging.
  - **Note**: keep placeholders only (e.g., `.gitkeep`) if runtime dirs must exist in empty form.

- **SQLite DBs (`*.db`, `*.sqlite*`)**
  - **Why**: can contain secrets/tokens/PII; binary; grows over time.
  - **Action**: delete + untrack; ignore; exclude from packaging.

- **Backups (`backups/`)**
  - **Why**: may include sensitive data; definitely not a source artifact.
  - **Action**: delete + untrack; ignore; exclude from packaging.

- **Test/sample media (`samples/*.mp4`)**
  - **Why**: size/licensing; not needed in runtime distribution.
  - **Action**: optional to keep tracked (if licensed) but **exclude from release zip**; consider Git LFS or download-on-demand in CI.

## Proposed `.gitignore` additions (delta)

Current `.gitignore` already includes `__pycache__/`, `*.pyc`, `Input/`, `Output/`, `_tmp*/`, `*.db`, and common caches.

Recommended additions to reduce future regressions:

```gitignore
# packaging/build output (generated)
build/
dist/

# local backups (never commit)
backups/

# sqlite variants (safety belt)
*.sqlite
*.sqlite3

# coverage outputs
.coverage
coverage.xml
htmlcov/

# local venvs
.venv/
venv/
ENV/

# OS/editor noise
.DS_Store
Thumbs.db
```

If you decide to keep runtime directories present but empty in git, add a placeholder allow rule pattern (example):

```gitignore
# keep placeholders only (optional)
!Input/.gitkeep
!Output/.gitkeep
```

## Proposed “release packaging allowlist” (what SHOULD go into a release zip)

Recommendation: switch release zips to an **allowlist-based package** (not “zip the repo”).

Include (allow):
- `src/**` (all Python source)
- `config/**` (excluding any secret material; `secret_config.py` should be safe-by-default or documented carefully)
- `projects/**`
- `scripts/**`
- `tools/**`
- `docs/**` (optional; include if docs are intended to ship)
- `docker/**` and `deploy/**` (optional; include if the release targets ops users)
- `tests/**` (usually exclude from runtime releases; include only if shipping as a dev SDK)
- Root files needed to run/install:
  - `pyproject.toml`
  - `README.md`, `README-deploy.md`
  - `.env.example`
  - `main.py` (if still used as an entrypoint)
  - `Makefile` (if present/required)

Exclude (deny), regardless of allowlist:
- `build/**`, `dist/**`, `**/*.egg-info/**`
- `**/__pycache__/**`, `**/*.pyc`
- `backups/**`
- `Input/**`, `Output/**`, `uploads/**`, `outputs/**`, `_tmp*/**`
- `**/*.db`, `**/*.sqlite*`
- Media unless explicitly intended: `**/*.wav`, `**/*.mp4`, `**/*.mkv`, `**/*.webm`
- Any release-time generated artifacts (SBOMs, logs) unless created by the release pipeline

## Proposed CI gates to prevent regressions

CI today runs tests via `make check` in `.github/workflows/ci.yml`. Add a lightweight “repo hygiene” gate that fails if forbidden artifacts are tracked.

Suggested checks:
- **Forbidden tracked file patterns** (fail build if any tracked path matches):
  - `(^|/)__pycache__/`
  - `\\.pyc$`
  - `^build/` or `^dist/`
  - `\\.egg-info/`
  - `^backups/`
  - `^_tmp` (all `_tmp*`)
  - `(^|/)Input/` and `(^|/)Output/` (except optional `.gitkeep`)
  - `\\.(db|sqlite|sqlite3)$`
- **Binary/media guardrails**:
  - If media fixtures are allowed, restrict them to a narrow allowlist path (e.g. `samples/**`) and fail on media elsewhere.
- **Size cap**:
  - Fail if any newly-added tracked file exceeds a threshold (e.g. 5–10MB) unless explicitly allowed (prevents accidental model/video uploads).

Implementation approach (CI step proposal, not applied yet):
- Add a small script (e.g. `scripts/verify_repo_hygiene.py`) that:
  - runs `git ls-files`
  - checks patterns + allowlist exceptions
  - exits non-zero with a clear message listing offending paths

## Recommended execution order (future PRs)

1) **Untrack + delete** the identified artifacts:
   - `build/lib/**`
   - all `__pycache__/` + `*.pyc`
   - `backups/**`
   - `_tmp*/**`
   - `Input/Test.mp4` (and any other runtime dir content)
   - any `*.db` / `*.sqlite*`
2) Add `.gitignore` delta above (or confirm existing rules cover everything).
3) Decide policy for `data/reports/**` and `voices/embeddings/Speaker1.npy`:
   - generated vs fixture vs downloadable asset
4) Add CI “repo hygiene” gate.
5) Define/implement the **release zip allowlist** in the release pipeline (or document the official release mechanism as Docker-only).

