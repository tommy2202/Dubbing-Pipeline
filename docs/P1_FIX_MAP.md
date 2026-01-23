# P1 Fix Map (UX + reliability)

Scope: **P1 items only** (no P0/v0, no P2).  
Goal: identify **canonical modules** and **exact files/endpoints/scripts** to extend
without duplicating systems or weakening security.

---

## Canonical modules (P1 scope)

### Uploads (resumable/mobile)
- **API + logic:** `src/dubbing_pipeline/web/routes_jobs.py`
  - `/api/uploads/init|{id}|{id}/chunk|{id}/complete` (resumable flow)
  - `/api/uploads/{id}` status (received bytes, per-chunk map)
- **Data store:** `src/dubbing_pipeline/jobs/store.py` (`put_upload`, `update_upload`, `get_upload`)
- **Filesystem layout:** `src/dubbing_pipeline/utils/paths.py` (`default_paths().uploads_dir`)
- **Config limits:** `config/public_config.py` (`max_upload_mb`, `upload_chunk_bytes`, input dirs)
- **UI entrypoint:** `src/dubbing_pipeline/web/routes_ui.py` (`/ui/upload`)
- **UI template:** `src/dubbing_pipeline/web/templates/upload_wizard.html`

### Jobs + logs (timeline, status, last log line)
- **Job model/state:** `src/dubbing_pipeline/jobs/models.py`
- **Job store + log helpers:** `src/dubbing_pipeline/jobs/store.py` (`append_log`, `tail_log`)
- **Checkpoint + stage data:** `src/dubbing_pipeline/jobs/checkpoint.py`
- **Per-job logs:** `src/dubbing_pipeline/utils/job_logs.py` (JSONL + summary)
- **Pipeline stage emissions:** `src/dubbing_pipeline/cli.py`, `src/dubbing_pipeline/jobs/queue.py`,
  `src/dubbing_pipeline/stages/*`
- **Job APIs:** `src/dubbing_pipeline/web/routes_jobs.py`
  - `/api/jobs/{id}` (includes checkpoint)
  - `/api/jobs/{id}/logs/tail|stream`
- **Job UI:** `src/dubbing_pipeline/web/templates/job_detail.html`,
  `src/dubbing_pipeline/web/templates/_jobs_table.html`

### Notifications (private ntfy)
- **Notifier:** `src/dubbing_pipeline/notify/ntfy.py`, `src/dubbing_pipeline/notify/base.py`
- **Call site:** `src/dubbing_pipeline/jobs/queue.py` (`_notify_job_finished`)
- **Config:** `config/public_config.py` (`NTFY_*`), `config/secret_config.py` (`NTFY_AUTH`)
- **Per-user settings store:** `src/dubbing_pipeline/api/routes_settings.py` (`UserSettingsStore`)
- **UI:** `src/dubbing_pipeline/web/templates/settings.html`,
  `src/dubbing_pipeline/web/routes_ui.py` (`/ui/settings`)
- **Docs + verify:** `docs/notifications.md`, `scripts/verify_ntfy.py`

### Runtime model detection / readiness
- **Model cache + load:** `src/dubbing_pipeline/runtime/model_manager.py`
- **Runtime APIs:** `src/dubbing_pipeline/api/routes_runtime.py`
  - `/api/runtime/models`, `/api/runtime/models/prewarm`, `/api/runtime/state`, `/api/runtime/queue`
- **Disk/space guard:** `src/dubbing_pipeline/ops/storage.py` (`ensure_free_space`)
- **UI:** `src/dubbing_pipeline/web/templates/models.html`,
  `src/dubbing_pipeline/web/routes_ui.py` (`/ui/models`)
- **Config:** `config/public_config.py` (`ENABLE_MODEL_DOWNLOADS`, `ALLOW_EGRESS`,
  `HF_HOME`, `TTS_HOME`, `TORCH_HOME`, `MIN_FREE_GB`)

### Web UI pages/templates
- **Router:** `src/dubbing_pipeline/web/routes_ui.py`
- **Templates:** `src/dubbing_pipeline/web/templates/*`
  - `upload_wizard.html`, `job_detail.html`, `dashboard.html`, `_jobs_table.html`,
    `models.html`, `settings.html`, `library_*`, etc.
- **Shared JS/CSS:** `src/dubbing_pipeline/web/templates/_base.html`

---

## P1 items → exact files / endpoints / integration notes

### 1) Mobile upload UX improvements (progress, retries, resume clarity)
**Primary files to modify**
- `src/dubbing_pipeline/web/routes_jobs.py`
  - Extend upload responses (status + chunk) to include **progress** and **next-missing** info.
  - Ensure logs include `request_id`, `user_id`, `upload_id` on init/chunk/complete.
- `src/dubbing_pipeline/jobs/store.py`
  - Use existing `received`/`received_bytes` map as single source of truth.
- `src/dubbing_pipeline/web/templates/upload_wizard.html`
  - Add mobile-friendly progress bar + retry state.
  - Clear “resume status” (chunks completed vs missing).
- `src/dubbing_pipeline/web/templates/_base.html`
  - Shared JS helpers for retry/backoff + toasts (reuse `showToast`).

**Endpoints/pages involved**
- `/ui/upload` (UI entry)
- `/api/uploads/init` → return `upload_id`, `chunk_bytes`, `max_upload_mb`
- `/api/uploads/{id}` → status (received_bytes, received map)
- `/api/uploads/{id}/chunk` → write chunk
- `/api/uploads/{id}/complete` → finalize

**Integration notes (no duplication)**
- Keep **existing resumable upload flow**; augment responses and UI only.
- Derive progress from `received_bytes / total_bytes` (server truth).
- Use the existing per-upload lock (`_upload_lock`) to avoid race conditions.
- Avoid any new upload storage system; `JobStore.uploads` is canonical.

---

### 2) Job timeline/stage progress UI (queued → extract → ASR → translate → TTS → mix → export)
**Primary files to modify**
- `src/dubbing_pipeline/web/routes_jobs.py`
  - Extend `/api/jobs/{id}` or add `/api/jobs/{id}/timeline` to expose:
    - **stage map** (from checkpoints)
    - **durations** (from `logs/summary.json`)
    - **last log line** (tail of `job.log_path`)
- `src/dubbing_pipeline/jobs/checkpoint.py`
  - Canonical stage completion info (`stages.*.done`, `done_at`).
- `src/dubbing_pipeline/utils/job_logs.py`
  - Use `summary.json` (`stage_durations_s`) + JSONL logs.
- `src/dubbing_pipeline/jobs/store.py`
  - `tail_log` already exists; reuse for last-line UX.
- `src/dubbing_pipeline/web/templates/job_detail.html`
  - Add timeline UI (stage list with durations + last log line).
- `src/dubbing_pipeline/web/templates/_jobs_table.html`
  - Optional: show last log line / stage status in job list cards.

**Stage mapping (existing sources)**
- Extract → `audio` (from `stages/audio_extractor.py`, checkpoint stage `"audio"`)
- ASR → `transcribe` (`stages/transcription.py`)
- Translate → `post_translate` (queue + streaming runner)
- TTS → `tts` (`stages/tts.py`)
- Mix → `mix` (`stages/mixing.py`)
- Export → `mkv_export` / `export` (`stages/mkv_export.py`, `stages/export.py`)
- Job logger emits `stage_start`, `stage_ok`, `stage_failed` to JSONL (`pipeline.log`)

**Endpoints/pages involved**
- `/api/jobs/{id}` (already returns `checkpoint`)
- `/api/jobs/{id}/logs/tail` (last log line)
- `/api/jobs/{id}/logs/stream` (live updates)
- `/ui/jobs/{id}` (job detail page)

**Integration notes**
- Reuse `JobLogger.write_summary()` output (`Output/<job>/logs/summary.json`)
  for **durations** rather than recomputing.
- Keep existing security: `require_job_access` on all job/log endpoints.
- Avoid new logging systems; reuse `JobLogger` + `JobStore.tail_log`.

---

### 3) Notifications via private self-hosted ntfy (per-user settings + deep links)
**Primary files to modify**
- `src/dubbing_pipeline/api/routes_settings.py`
  - Extend `UserSettingsStore` defaults to include `notifications` section
    (e.g., enabled flag, topic override, priority).
  - Extend `/api/settings` GET/PUT payloads to include per-user notification prefs.
- `src/dubbing_pipeline/jobs/queue.py`
  - `_notify_job_finished` should read **per-user settings** (if present).
  - Include deep link URL if `PUBLIC_BASE_URL` configured (already used).
- `src/dubbing_pipeline/notify/ntfy.py`
  - Remains the canonical sender; no new notifier class.
- `src/dubbing_pipeline/web/templates/settings.html`
  - UI controls for user notification prefs (opt-in, topic override).
- `docs/notifications.md`
  - Update for per-user topic/settings guidance.

**Endpoints/pages involved**
- `/api/settings` (read/write per-user prefs)
- `/ui/settings` (UI)
- `notify.ntfy` call site in job completion

**Integration notes**
- Do not introduce new notification systems; **extend ntfy only**.
- Keep secrets out of UI/APIs (never expose `NTFY_AUTH`).
- Deep links should point to `/ui/jobs/{id}` with `PUBLIC_BASE_URL` (existing pattern).
- Log events must include `request_id`, `user_id`, `job_id`.

---

### 4) Model readiness dashboard (installed/enabled and WHY not)
**Primary files to modify**
- `src/dubbing_pipeline/api/routes_runtime.py`
  - Extend `/api/runtime/models` response to include readiness reasons:
    - whisper installed? tts installed?
    - egress disabled? downloads disabled?
    - disk space constraints?
    - license/TOS requirements?
- `src/dubbing_pipeline/runtime/model_manager.py`
  - Add **non-loading** checks for availability (import checks only).
- `src/dubbing_pipeline/web/templates/models.html`
  - Add readiness section: list models, enabled/disabled, reason text.
- `config/public_config.py`
  - Ensure config flags are surfaced in readiness response (no secrets).

**Endpoints/pages involved**
- `/api/runtime/models` (admin)
- `/ui/models`

**Integration notes**
- Reuse `ModelManager` (no new cache).
- Avoid triggering downloads: use import checks only.
- Keep reasons **non-sensitive** (no URLs/credentials).

---

### 5) E2E reliability gates (no GPU required)
**Primary scripts**
- Existing:
  - `scripts/e2e_concurrency_two_users.py` (two users submit)
  - `scripts/e2e_upload_resume.py` (interrupted/resumed upload)
  - `scripts/e2e_job_recovery.py` (restart worker mid-run)
  - `scripts/e2e_smoke_web.py` (cancel flow; uses fallback queue)
  - `scripts/verify_queue_fallback.py` (redis down → fallback)
- Missing / to add:
  - `scripts/e2e_cancel_midrun.py` (explicit cancel while RUNNING)
  - `scripts/e2e_redis_reconnect.py` (redis down → fallback → redis back)

**Integration points**
- Queue backend status: `src/dubbing_pipeline/queue/redis_queue.py`,
  `src/dubbing_pipeline/queue/fallback_local_queue.py`
- Scheduler/queue wiring: `src/dubbing_pipeline/runtime/scheduler.py`,
  `src/dubbing_pipeline/jobs/queue.py`

**CI hooks**
- `/.github/workflows/ci-core.yml` (nightly reliability job)
  - Add new P1 scripts here (no GPU required).

---

### 6) Release hygiene: CI “pro steps” degrade gracefully when keys/env missing
**Primary files to modify**
- `.github/workflows/ci-release.yml`
  - Keep key detection + conditional signing/attestation steps.
  - Ensure explicit “skipped due to missing keys” log lines.
- (Optional) `scripts/package_release.py` if used by CI release steps.

**Integration notes**
- No secret logging; do not echo keys.
- Build/scan must still run even if signing keys are missing.
- Keep signing steps behind `if: steps.keys.outputs.present == 'true'`.

---

## Logging requirements (apply across P1)
- Include `request_id`, `user_id`, `job_id`, `upload_id` in:
  - upload endpoints (`routes_jobs.py`)
  - job timeline/log endpoints
  - notification send paths (`jobs/queue.py`, `notify/ntfy.py`)
- Reuse existing `safe_log` / `audit_event` patterns; do not add new loggers.

