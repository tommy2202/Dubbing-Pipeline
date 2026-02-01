# Refactor Targets Report

Criteria: Python files > 800 lines or multi-responsibility (CLI mega-file, review mega-route, queue manager).

## Summary (line counts)

| File | Lines |
| --- | ---: |
| src/dubbing_pipeline/jobs/queue.py | 4420 |
| src/dubbing_pipeline/cli.py | 3265 |
| src/dubbing_pipeline/jobs/store.py | 2694 |
| src/dubbing_pipeline/stages/tts.py | 1509 |
| src/dubbing_pipeline/web/routes/jobs_review.py | 1144 |
| src/dubbing_pipeline/qa/scoring.py | 1006 |
| src/dubbing_pipeline/queue/redis_queue.py | 914 |
| src/dubbing_pipeline/api/routes/admin_actions.py | 807 |

## src/dubbing_pipeline/jobs/queue.py (4420 lines)

**Responsibilities detected**
- JobQueue lifecycle, worker loop, cancellation, pause/resume.
- Job state transitions, log appends, checkpoint handling, progress updates.
- End-to-end pipeline orchestration (audio extraction, diarization, transcription, translation, TTS, mixing, mux).
- Output path setup and runtime metadata (including two-pass cloning setup).
- Privacy controls, retention policy handling, QA scoring, drift reports, library mirroring.
- Metrics/audit emission and scheduler/queue-backend coordination.

**Proposed split plan (new module names)**
- `dubbing_pipeline/jobs/queue.py`: keep `JobQueue` orchestration + enqueue/cancel API.
- `dubbing_pipeline/jobs/pipeline/context.py`: resolve output paths, runtime, privacy flags.
- `dubbing_pipeline/jobs/pipeline/checkpoints.py`: checkpoint read/write + stage start/skip helpers.
- `dubbing_pipeline/jobs/pipeline/stages/audio.py`
- `dubbing_pipeline/jobs/pipeline/stages/diarize.py`
- `dubbing_pipeline/jobs/pipeline/stages/transcribe.py`
- `dubbing_pipeline/jobs/pipeline/stages/translate.py`
- `dubbing_pipeline/jobs/pipeline/stages/tts.py`
- `dubbing_pipeline/jobs/pipeline/stages/mix.py`
- `dubbing_pipeline/jobs/pipeline/stages/mux.py`
- `dubbing_pipeline/jobs/pipeline/postprocess.py`: lipsync, QA scoring, drift reports, retention.
- `dubbing_pipeline/jobs/pipeline/telemetry.py`: metrics, audit, job log helpers.

**Public API that must remain stable**
- `dubbing_pipeline.jobs.queue.JobQueue` (class and method signatures).
- `dubbing_pipeline.jobs.queue.JobCanceled` (exception).
- `JobQueue.start/stop/graceful_shutdown/enqueue/enqueue_id/cancel/kill/pause/resume` behavior and names.
- Import path must remain stable for `server.py`, `scheduler.py`, and scripts/tests.

## src/dubbing_pipeline/cli.py (3265 lines) — CLI mega-file

**Responsibilities detected**
- Click CLI setup, argument/option parsing, default command routing.
- Full pipeline execution for single and batch jobs (audio, diarize, transcribe, translate, TTS, mix, mux).
- Output path setup, subtitles writing, manifest generation.
- Post-processing: lipsync plugin, retention cleanup, QA scoring, drift reports.
- Logging, job summaries, library mirroring for CLI runs.

**Proposed split plan (new module names)**
- `dubbing_pipeline/cli.py`: keep `cli` group, command registration, and minimal wrappers.
- `dubbing_pipeline/cli/commands/run.py`: `run` command and Click option definitions.
- `dubbing_pipeline/cli/pipeline.py`: pipeline orchestration for CLI runs.
- `dubbing_pipeline/cli/postprocess.py`: lipsync, retention, QA, drift reporting.
- `dubbing_pipeline/cli/io.py`: SRT/VTT helpers, output writing utilities.

**Public API that must remain stable**
- `dubbing_pipeline.cli:cli` entrypoint (used by `pyproject.toml` scripts).
- `DefaultGroup` class.
- `run` command signature and Click option names.
- Module path `dubbing_pipeline.cli` for `__main__` and scripts.

## src/dubbing_pipeline/jobs/store.py (2694 lines)

**Responsibilities detected**
- Core `JobStore` SQLite persistence (jobs, idempotency, presets, projects, uploads).
- Schema initialization/migrations for multiple domains: library, voice profiles, glossaries,
  pronunciation, QA reviews, reports, storage accounting, quotas, view history.
- Domain-level CRUD operations for each schema area.
- Job log handling and convenience queries.

**Proposed split plan (new module names)**
- `dubbing_pipeline/jobs/store.py`: keep `JobStore` interface and job CRUD.
- `dubbing_pipeline/jobs/store/schema.py`: schema init helpers (or split per domain).
- `dubbing_pipeline/jobs/store/library.py`
- `dubbing_pipeline/jobs/store/voice_profiles.py`
- `dubbing_pipeline/jobs/store/glossaries.py`
- `dubbing_pipeline/jobs/store/pronunciation.py`
- `dubbing_pipeline/jobs/store/qa.py`
- `dubbing_pipeline/jobs/store/reports.py`
- `dubbing_pipeline/jobs/store/storage.py`
- `dubbing_pipeline/jobs/store/quotas.py`
- `dubbing_pipeline/jobs/store/uploads.py`
- `dubbing_pipeline/jobs/store/presets.py`
- `dubbing_pipeline/jobs/store/projects.py`

**Public API that must remain stable**
- `dubbing_pipeline.jobs.store.JobStore` class (constructor + current method names).
- Job CRUD APIs: `put/get/update/list/list_all/delete_job`.
- Log APIs: `append_log/tail_log`.
- Public domain methods used across API/routes/tests (e.g., library, voice profiles, quotas).

## src/dubbing_pipeline/stages/tts.py (1509 lines)

**Responsibilities detected**
- TTS synthesis orchestration, provider selection, caching, retry logic.
- SRT parsing, timing alignment, clip stitching, silence generation.
- Voice selection/cloning, two-pass orchestration hooks, emotion controls.
- Manifest/output file writing and fallbacks (espeak-ng).

**Proposed split plan (new module names)**
- `dubbing_pipeline/stages/tts.py`: keep `run` entrypoint + high-level orchestration.
- `dubbing_pipeline/stages/tts/parse.py`: SRT parsing helpers.
- `dubbing_pipeline/stages/tts/voice_selection.py`: choose voice/clone mapping logic.
- `dubbing_pipeline/stages/tts/synthesize.py`: clip synthesis loop and retries.
- `dubbing_pipeline/stages/tts/manifest.py`: manifest read/write + output bookkeeping.
- `dubbing_pipeline/stages/tts/fallbacks.py`: espeak and silence helpers.

**Public API that must remain stable**
- `dubbing_pipeline.stages.tts.run`
- `dubbing_pipeline.stages.tts.render_aligned_track`
- `dubbing_pipeline.stages.tts._write_silence_wav` (used by streaming/review helpers).
- `dubbing_pipeline.stages.tts._espeak_fallback` (used by voice audition).
- `dubbing_pipeline.stages.tts.TTSCanceled`

## src/dubbing_pipeline/web/routes/jobs_review.py (1144 lines) — review mega-route

**Responsibilities detected**
- Review state management (init/load state, segment extraction, QA annotations).
- Transcript/segment overrides, approval/reject flows, segment regen.
- Review helper actions (rewrite helpers, edit helpers).
- Review audio streaming and file access checks.
- Job resubmit / queue interactions for review changes.

**Proposed split plan (new module names)**
- `dubbing_pipeline/web/routes/jobs_review.py`: keep router and thin wrappers to preserve route snapshot.
- `dubbing_pipeline/web/routes/review/overrides.py`
- `dubbing_pipeline/web/routes/review/segments.py`
- `dubbing_pipeline/web/routes/review/transcript.py`
- `dubbing_pipeline/web/routes/review/helpers.py`
- `dubbing_pipeline/web/routes/review/audio.py`
- `dubbing_pipeline/web/routes/review/state.py`

**Public API that must remain stable**
- `router` object and all existing handler function names in this module
  (route snapshot includes module + endpoint name).
- Route paths and HTTP methods must remain unchanged.

## src/dubbing_pipeline/qa/scoring.py (1006 lines)

**Responsibilities detected**
- QA scoring rules and issue creation.
- Audio metrics (duration, peak), text metrics, overlap detection.
- Segment loading from review state, translated files, and stream manifests.
- Summary/report output generation.

**Proposed split plan (new module names)**
- `dubbing_pipeline/qa/scoring.py`: keep `score_job` + dataclasses.
- `dubbing_pipeline/qa/inputs.py`: load segments, manifests, and review state.
- `dubbing_pipeline/qa/metrics.py`: audio/text metrics helpers.
- `dubbing_pipeline/qa/rules.py`: issue rules and scoring logic.
- `dubbing_pipeline/qa/reporting.py`: write summary outputs.
- `dubbing_pipeline/qa/manifests.py`: manifest lookup helpers.

**Public API that must remain stable**
- `dubbing_pipeline.qa.scoring.score_job`
- `dubbing_pipeline.qa.scoring.find_latest_tts_manifest_path`
- `QAIssue` and `SegmentQA` dataclasses.

## src/dubbing_pipeline/queue/redis_queue.py (914 lines) — queue manager

**Responsibilities detected**
- Redis-backed queue backend (submit, claim, cancel, ack).
- Distributed locks, health/consume/delayed loops, job metadata in Redis.
- Admin snapshots and quota enforcement hooks.
- Audit/logging/metrics for queue events.

**Proposed split plan (new module names)**
- `dubbing_pipeline/queue/redis_queue.py`: keep `RedisQueue` public surface.
- `dubbing_pipeline/queue/redis_keys.py`: Redis key naming helpers.
- `dubbing_pipeline/queue/redis_scripts.py`: Lua scripts and registration.
- `dubbing_pipeline/queue/redis_workers.py`: health/consume/delayed loops.
- `dubbing_pipeline/queue/redis_admin.py`: admin snapshot helpers.

**Public API that must remain stable**
- `dubbing_pipeline.queue.redis_queue.RedisQueue`
- `dubbing_pipeline.queue.redis_queue.RedisQueueConfig`
- RedisQueue method names used by `queue.manager`/queue backend wrappers.

## src/dubbing_pipeline/api/routes/admin_actions.py (807 lines)

**Responsibilities detected**
- Admin queue introspection and job control (priority, cancel, visibility).
- Glossary/pronunciation CRUD for admin tools.
- Admin metrics and job failure summaries.
- Voice profile suggestion approvals and invite management.
- Per-user queue quotas via Redis backend.

**Proposed split plan (new module names)**
- `dubbing_pipeline/api/routes/admin_actions.py`: keep router and thin wrappers.
- `dubbing_pipeline/api/admin/queue.py`
- `dubbing_pipeline/api/admin/jobs.py`
- `dubbing_pipeline/api/admin/quotas.py`
- `dubbing_pipeline/api/admin/glossaries.py`
- `dubbing_pipeline/api/admin/pronunciation.py`
- `dubbing_pipeline/api/admin/metrics.py`
- `dubbing_pipeline/api/admin/voices.py`
- `dubbing_pipeline/api/admin/invites.py`

**Public API that must remain stable**
- `router` object and existing handler function names
  (route snapshot includes module + endpoint name).
- Route paths and HTTP methods must remain unchanged.
