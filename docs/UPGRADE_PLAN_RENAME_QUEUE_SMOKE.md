# Upgrade plan: rename sweep + queue unification + smoke test

This document captures the remaining marker scan, usage analysis, queue
entrypoints, and the planned upgrade steps. It is intentionally ASCII-only;
snippets are omitted where they would introduce non-ASCII characters.

---

## 1) Marker scan results (task 1)

Search tokens: `anime`, `anime_v2`, `ANV2`, `v1`, `v2`, `alpha`.
Lists below include file and line numbers only.

### `anime`
- docs/vnext_done.md: 96
- src/dubbing_pipeline/security/crypto.py: 262

### `anime_v2`
- No matches

### `ANV2`
- tests/test_char_store_encryption.py: 49
- src/dubbing_pipeline/security/crypto.py: 24
- src/dubbing_pipeline/stages/character_store.py: 13

### `v1`
- scripts/verify_two_pass_voice_clone.py: 155, 160
- src/dubbing_pipeline_legacy/stages/mkv_export.py: 43
- src/dubbing_pipeline/reports/drift.py: 324, 326
- scripts/verify_qa.py: 89, 103, 111
- scripts/verify_library_endpoints_sorting.py: 132, 241
- docs/repo_cleanup_plan.md: 387, 482

### `v2`
- src/dubbing_pipeline/cli.py: 18, 633, 638, 639, 655, 877, 878, 1134, 1135, 1837, 3041, 3044, 3049, 3050
- src/dubbing_pipeline/jobs/queue.py: 41, 1120, 3575, 3579, 3580, 3585, 3586
- src/dubbing_pipeline/api/routes_system.py: 424, 427, 432, 434, 440, 442, 443, 444, 449, 451, 453
- config/public_config.py: 286, 359, 361, 362
- README.md: 660, 663, 664, 669, 681, 690
- docs/voice_clone_persistence_plan.md: 72, 77
- scripts/download_models.py: 15, 16, 18, 20, 22, 77, 83, 85, 93, 110
- scripts/verify_two_pass_voice_clone.py: 172, 178
- src/dubbing_pipeline_legacy/stages/lipsync.py: 18, 19, 20, 23, 39
- scripts/smoke_import_all.py: 49
- src/dubbing_pipeline_legacy/stages/tts.py: 216
- src/dubbing_pipeline/stages/tts_engine.py: 48
- src/dubbing_pipeline/security/crypto.py: 262
- src/dubbing_pipeline/api/routes_runtime.py: 118
- .env.example: 4, 132
- docker/Dockerfile: 37
- scripts/train_voice.py: 42
- docs/next_version_plan.md: 32
- docs/repo_cleanup_plan.md: 262
- docs/CLI.md: 228, 229, 230
- docs/FEATURES.md: 219
- scripts/verify_lipsync_plugin.py: 9, 11, 12, 50, 57, 58, 59, 73, 75, 120
- scripts/verify_library_endpoints_sorting.py: 145, 230, 241
- scripts/verify_no_anime_or_versions.py: 20
- src/dubbing_pipeline/voice_memory/embeddings.py: 87, 91, 93, 110, 112, 113, 115
- src/dubbing_pipeline/plugins/lipsync/wav2lip_plugin.py: 17, 24, 33, 35, 43, 44, 45, 46, 47, 114, 115, 120, 123, 126, 130, 131, 133, 144, 156, 163, 174, 175, 178, 187, 190, 201, 236, 238, 256, 259, 480, 515, 550, 641, 680, 749, 750, 751, 752
- src/dubbing_pipeline/plugins/lipsync/registry.py: 6, 12, 13, 18, 19
- src/dubbing_pipeline/plugins/lipsync/preview.py: 59, 61, 63, 115, 116, 127, 131, 142, 150

### `alpha`
- tests/test_library_search_sort.py: 88, 138, 166
- src/dubbing_pipeline/utils/crypto.py: 15, 16
- scripts/verify_library_browse.py: 112, 151

---

## 2) Marker usage analysis (task 2)

Key uses of the markers above (encryption headers, store keys, temp prefixes):

- `src/dubbing_pipeline/security/crypto.py`
  - `MAGIC = b"ANV2ENC"` is the at-rest encryption header for chunked AES-GCM
    files. `is_encrypted_path()` reads the header and `decrypt_file()` requires it.
  - `materialize_decrypted()` uses `tempfile.mkstemp(prefix="animev2_dec_", ...)`
    for decrypted temp files.
- `src/dubbing_pipeline/stages/character_store.py`
  - `_MAGIC = b"ANV2CHAR"` is the encrypted CharacterStore header.
  - `_AAD` embeds a version token (`... + b"v" + b"1"`) for AES-GCM AAD.
  - `_FORMAT_VERSION = 1` is the CharacterStore file format version.
- `tests/test_char_store_encryption.py`
  - Asserts `ANV2CHAR` header presence.

Other `v1` / `v2` / `alpha` hits are mostly incidental identifiers:
- Model/plugin names and dependencies (`xtts_v2`, `wav2lip`, `cv2`).
- Test IDs/fixtures (`j_ep2_v1`, `1_v1.wav`, `series_slug="alpha"`).
- Internal variable names (`pv1`, `prev2`, `alphabet`).

---

## 3) Current queue entrypoints (task 3)

### Enqueue sources (API/UI and internal)

API/UI routes (FastAPI):
- `src/dubbing_pipeline/web/routes/jobs_submit.py`
  - `/api/jobs` submission path uses `queue_backend.submit_job()` when available
    (line ~569) with a fallback to `scheduler.submit()` when absent.
  - `/api/jobs/batch` uses the same pattern in `_submit_one()` (line ~761) and
    other batch helpers.
- `src/dubbing_pipeline/web/routes/jobs_actions.py`
  - `/api/jobs/{id}/resume` requeues via `queue_backend.submit_job()` with
    fallback to `scheduler.submit()` (lines ~136-155).
- `src/dubbing_pipeline/web/routes/jobs_review.py`
  - `/api/jobs/{id}/transcript/synthesize` requeues via `queue_backend.submit_job()`
    with fallback to `scheduler.submit()` (lines ~505-524).
- `src/dubbing_pipeline/web/routes/admin.py`
  - Two-pass rerun path requeues via `queue_backend.submit_job()` with fallback to
    `scheduler.submit()` (lines ~134-156).

Internal paths:
- `src/dubbing_pipeline/jobs/queue.py`
  - Two-pass voice clone pass2 requeue uses `queue_backend.submit_job()` when
    present, otherwise scheduler/JobQueue directly (lines ~3368-3396).
- `src/dubbing_pipeline/runtime/scheduler.py`
  - `Scheduler.submit()` adds to a priority heap and dispatcher thread enqueues
    into `JobQueue` via `_enqueue_cb` (lines ~84-105, ~172+).

### Worker execution path

- `src/dubbing_pipeline/server.py`
  - Creates `JobQueue`, `Scheduler`, and `queue_backend` and wires callbacks
    (lines ~114-149).
- `src/dubbing_pipeline/runtime/lifecycle.py`
  - `start_all()` starts `queue_backend` and `job_queue` (lines ~290-297).
- `src/dubbing_pipeline/jobs/queue.py`
  - `JobQueue.start()` spins worker tasks that execute jobs.
- `src/dubbing_pipeline/queue/redis_queue.py`
  - `_consume_loop()` claims jobs from Redis and calls `enqueue_job_id_cb` to
    feed `JobQueue` (lines ~575-620).

### Redis vs local selection

- `config/public_config.py`
  - `QUEUE_MODE` (`auto|redis|fallback`) and `QUEUE_BACKEND` (`local|redis`).
- `config/secret_config.py`
  - `REDIS_URL` enables Redis.
- `src/dubbing_pipeline/queue/queue_backend.py`
  - `build_queue_backend()` maps `QUEUE_BACKEND` to a mode override.
- `src/dubbing_pipeline/queue/manager.py`
  - `AutoQueueBackend` chooses Redis vs fallback at runtime using `queue_mode`,
    `queue_backend`, and `redis_url`, and routes submit/cancel/quotas accordingly.
- `src/dubbing_pipeline/queue/fallback_local_queue.py`
  - Fallback submission calls `Scheduler.submit()` and uses a local scan loop.

---

## 4) Exact rename targets (task 4a)

Targets tied to "anime"/version naming in code paths, temp prefixes, or markers:

1) Encryption headers and file markers
   - `src/dubbing_pipeline/security/crypto.py`: `MAGIC = b"ANV2ENC"`
   - `src/dubbing_pipeline/stages/character_store.py`: `_MAGIC = b"ANV2CHAR"`
   - `tests/test_char_store_encryption.py`: asserts `ANV2CHAR`

2) Temp file prefix
   - `src/dubbing_pipeline/security/crypto.py`: `prefix="animev2_dec_"`

3) AAD / format identifiers
   - `src/dubbing_pipeline/stages/character_store.py`:
     `_AAD = b"...:" + b"v" + b"1"`

4) Legacy / test IDs and file names (keep or rename depending on policy)
   - `src/dubbing_pipeline_legacy/stages/mkv_export.py`: `v1_output_dir`
   - `scripts/verify_qa.py`: `review/audio/1_v1.wav`
   - `scripts/verify_library_endpoints_sorting.py`: `j_ep2_v1`, `j_ep2_v2`
   - `docs/repo_cleanup_plan.md`: `1_v1.wav`

Non-target by design (still in scan results but not pipeline naming):
- Model versions: `xtts_v2` (README, config, env, Dockerfile, runtime routes).
- Plugin names: `wav2lip` and `Wav2Lip` identifiers.
- Library/test values: `series_slug="alpha"`, `alphabet` in utils/crypto.
- Generic variables: `pv1`, `prev2`, `cv2` in OpenCV helpers.

---

## 5) Backward compatibility strategy (task 4b)

For on-disk artifacts that already use old markers:

- Encryption header:
  - Accept both old and new headers in `is_encrypted_path()` and `decrypt_file()`.
  - Write only the new header for newly encrypted files.
- CharacterStore:
  - Accept both old and new `_MAGIC` values.
  - Attempt decrypt using the new AAD first; if it fails, retry with legacy AAD.
  - On `save()`, always write the new header/AAD, effectively migrating.
- Temp prefixes:
  - Switch to new prefix for newly materialized files.
  - Keep cleanup tolerant of old prefix if needed for legacy temp files.
- Legacy filenames (if renamed):
  - Read/lookup both old and new names for a transition period; prefer writing
    the new names, but keep fallback discovery of old names.

---

## 6) Queue unification plan (task 4c)

Goal: API/UI always enqueue through one facade, with no parallel execution paths.

Plan:
1) Make `AutoQueueBackend` the single source of truth.
   - Ensure `app.state.queue_backend` is always set (including tests).
2) Replace all direct `scheduler.submit()` fallbacks in API/UI endpoints with
   `queue_backend.submit_job()` calls.
3) Centralize requeue/resume logic:
   - Provide a small helper (e.g., `queue.submit_or_raise(...)`) to standardize
     submission metadata and error handling.
4) Keep existing backends:
   - RedisQueue remains the L2 backend when healthy.
   - FallbackLocalQueue remains the L1 backend, but it is only reached via the
     same `AutoQueueBackend` interface.
5) Add regression coverage to enforce one path:
   - Test that submission routes call the backend facade and not the scheduler
     directly (mock/spy or instrumentation).

---

## 7) Fresh machine smoke test plan (task 4d)

Location:
- Add a new pytest module, e.g. `tests/test_smoke_fresh_machine.py`.
- Reuse existing helpers (or factor one shared helper) for tiny MP4 creation to
  avoid duplicate code (see `tests/test_presets_projects_batch.py`).

Flow (deterministic, CPU-only):
1) Skip cleanly if `ffmpeg` is not on PATH with a clear message:
   "ffmpeg not available; install ffmpeg to run this smoke test".
2) Create a 1-2s synthetic MP4 using `ffmpeg` (`testsrc` + `anullsrc`).
3) Set runtime env vars for isolated temp dirs:
   `APP_ROOT`, `INPUT_DIR`, `DUBBING_OUTPUT_DIR`, `DUBBING_LOG_DIR`, admin creds.
4) Start minimal server components using `fastapi.TestClient(app)`.
5) Submit a low-mode, CPU job through `/api/jobs`.
6) Poll job status until DONE or timeout (deterministic upper bound).
7) Assert:
   - Output artifact exists (e.g., job output MP4/MKV in Output/ or via API file listing).
   - Library/job manifest is written and updated (`manifest.json` present).
   - Job visibility default is `private` (job field or manifest visibility).

Notes:
- No GPU required.
- Use existing manifest helpers (`library/manifest.py`) for consistency.
- Keep logging/redaction policies unchanged.

