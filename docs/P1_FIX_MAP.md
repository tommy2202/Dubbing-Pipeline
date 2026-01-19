# P1 Fix Map (UX + Ops) — plan only

Scope: P1 items only. No P0 security boundary changes; reuse existing modules.

## A) Canonical modules (by feature area)

### Upload routes + client JS
- Server upload API: `src/dubbing_pipeline/web/routes_jobs.py` (`/api/uploads/*`, upload init/chunk/complete, status, file picker).【F:src/dubbing_pipeline/web/routes_jobs.py†L1103-L1311】
- Client upload wizard + JS: `src/dubbing_pipeline/web/templates/upload_wizard.html` (resumable chunked upload + job submit).【F:src/dubbing_pipeline/web/templates/upload_wizard.html†L469-L605】

### Job model + status + logs + SSE
- Job model + fields: `src/dubbing_pipeline/jobs/models.py`.【F:src/dubbing_pipeline/jobs/models.py†L10-L94】
- Job lifecycle + pipeline stages: `src/dubbing_pipeline/jobs/queue.py` (stage execution, checkpointing, logs).【F:src/dubbing_pipeline/jobs/queue.py†L95-L170】【F:src/dubbing_pipeline/jobs/queue.py†L3715-L3777】
- Checkpoint schema (stage done + done_at): `src/dubbing_pipeline/jobs/checkpoint.py`.【F:src/dubbing_pipeline/jobs/checkpoint.py†L92-L128】
- Job logs APIs: `src/dubbing_pipeline/web/routes_jobs.py` (`/api/jobs/{id}/logs/tail`, `/logs/stream` SSE).【F:src/dubbing_pipeline/web/routes_jobs.py†L3003-L3061】
- UI: dashboard SSE updates and job detail view.
  - Dashboard SSE `/api/jobs/events` updates state/progress: `src/dubbing_pipeline/web/templates/dashboard.html`.【F:src/dubbing_pipeline/web/templates/dashboard.html†L149-L170】
  - Job detail stage breakdown uses `job.checkpoint.stages`: `src/dubbing_pipeline/web/templates/job_detail.html`.【F:src/dubbing_pipeline/web/templates/job_detail.html†L85-L111】
  - Jobs table/cards: `src/dubbing_pipeline/web/templates/_jobs_table.html`.【F:src/dubbing_pipeline/web/templates/_jobs_table.html†L1-L126】

### Notification subsystem
- Ntfy sender + audit/logging: `src/dubbing_pipeline/notify/ntfy.py`.【F:src/dubbing_pipeline/notify/ntfy.py†L98-L200】
- Job-finish hook: `src/dubbing_pipeline/jobs/queue.py` (`_notify_job_finished`).【F:src/dubbing_pipeline/jobs/queue.py†L3715-L3777】
- Per-user settings store: `src/dubbing_pipeline/api/routes_settings.py` (defaults only today; notifications are informational).【F:src/dubbing_pipeline/api/routes_settings.py†L31-L170】
- Settings UI: `src/dubbing_pipeline/web/templates/settings.html` (notifications section is read-only).【F:src/dubbing_pipeline/web/templates/settings.html†L138-L147】
- Docs: `docs/notifications.md` (server-level config).【F:docs/notifications.md†L1-L119】

### Model detection / gates
- Model cache + loader: `src/dubbing_pipeline/runtime/model_manager.py`.【F:src/dubbing_pipeline/runtime/model_manager.py†L27-L238】
- Runtime models API: `src/dubbing_pipeline/api/routes_runtime.py` (`/api/runtime/models`).【F:src/dubbing_pipeline/api/routes_runtime.py†L46-L96】
- Models UI page: `src/dubbing_pipeline/web/templates/models.html`.【F:src/dubbing_pipeline/web/templates/models.html†L1-L127】
- Config flags for feature enablement (Whisper, TTS, demucs, diarization, TOS): `config/public_config.py`.【F:config/public_config.py†L204-L287】

### UI templates/pages and routing
- UI route registry + page rendering: `src/dubbing_pipeline/web/routes_ui.py`.【F:src/dubbing_pipeline/web/routes_ui.py†L21-L200】
- Core pages: `src/dubbing_pipeline/web/templates/*.html` (dashboard, job detail, upload wizard, models, settings).

### Existing verify_* and reliability scripts
- Concurrency / two users: `scripts/e2e_concurrency_two_users.py`.【F:scripts/e2e_concurrency_two_users.py†L9-L110】
- Upload resume: `scripts/e2e_upload_resume.py`.【F:scripts/e2e_upload_resume.py†L102-L207】
- Job recovery after restart: `scripts/e2e_job_recovery.py`.【F:scripts/e2e_job_recovery.py†L26-L105】
- Redis queue health + fallback: `scripts/verify_queue_redis.py`, `scripts/verify_queue_fallback.py`.【F:scripts/verify_queue_redis.py†L119-L160】【F:scripts/verify_queue_fallback.py†L19-L56】
- Cancel path (smoke): `scripts/e2e_smoke_web.py` (submit + cancel).【F:scripts/e2e_smoke_web.py†L181-L204】
- Model API smoke: `scripts/verify_model_manager.py`.【F:scripts/verify_model_manager.py†L16-L45】
- Artifact hygiene: `scripts/check_no_tracked_artifacts.py`.【F:scripts/check_no_tracked_artifacts.py†L7-L67】
- CI release signing already skips if keys missing: `.github/workflows/ci-release.yml`.【F:.github/workflows/ci-release.yml†L94-L145】

## B) P1 Fix Map — file-by-file plan (no code changes yet)

### 1) Mobile upload UX improvements (progress, retries, resume clarity)
Existing partials:
- Upload Wizard uses chunked upload, checksum headers, and job creation, but no visible progress or resume UI.【F:src/dubbing_pipeline/web/templates/upload_wizard.html†L469-L605】
- Upload status endpoint exists for resumable state checks (`/api/uploads/{id}`), plus init/chunk/complete on server routes.【F:src/dubbing_pipeline/web/routes_jobs.py†L1103-L1311】

Plan (minimal changes, reuse current APIs):
- `src/dubbing_pipeline/web/templates/upload_wizard.html`
  - Add progress UI (percent, bytes sent) tied to chunk loop.
  - Persist upload session in `localStorage` (upload_id, file name/size, last index) so a refresh can resume.
  - Add retry/backoff for chunk failures (bounded attempts) and surface error/retry CTA.
  - Add resume clarity: show “Resuming upload” banner when a stored upload_id matches file metadata.
- `src/dubbing_pipeline/web/routes_jobs.py`
  - Optional small helper endpoint (if needed) to fetch resumable status by upload_id (or reuse existing `/api/uploads/{id}`).
  - Ensure any new responses include request_id/job_id in logs (no new security logic).

### 2) Job timeline/stage progress UI (stage durations + last log line)
Existing partials:
- Job detail shows checkpoint stages with `done` + `done_at`, but no durations and no stage ordering UI.【F:src/dubbing_pipeline/web/templates/job_detail.html†L85-L111】
- Logs tail + SSE endpoints exist for job logs (`/logs/tail`, `/logs/stream`).【F:src/dubbing_pipeline/web/routes_jobs.py†L3003-L3061】
- Dashboard SSE updates job state/progress only (no stage timeline).【F:src/dubbing_pipeline/web/templates/dashboard.html†L149-L168】

Plan (reuse checkpoint + logs):
- `src/dubbing_pipeline/jobs/checkpoint.py`
  - Extend checkpoint entries to include `started_at` timestamps (and optional `duration_s`).
- `src/dubbing_pipeline/jobs/queue.py`
  - Record stage start/end in checkpoint meta at each stage boundary (extracting, ASR, translate, TTS, mixing, export).
  - Append concise stage markers to job log for last-line derivation (include request_id/job_id in structured logs).
- `src/dubbing_pipeline/web/routes_jobs.py`
  - Extend job detail API payload to include `timeline` summary (stage order, started/done/duration, last_log_line).
  - Optional new endpoint `/api/jobs/{id}/timeline` if keeping payloads minimal.
- `src/dubbing_pipeline/web/templates/job_detail.html`
  - Add timeline UI: stage list with durations + last log line snapshot.
- `src/dubbing_pipeline/web/templates/_jobs_table.html`
  - Optional: show current stage in list view (small badge).

### 3) Notifications via private ntfy with per-user settings + deep links
Existing partials:
- Global ntfy sender and audit logging already wired on job finish.【F:src/dubbing_pipeline/notify/ntfy.py†L98-L200】【F:src/dubbing_pipeline/jobs/queue.py†L3715-L3777】
- Settings UI only displays server-level status; no per-user settings today.【F:src/dubbing_pipeline/web/templates/settings.html†L138-L147】

Plan (per-user config without loosening security):
- `src/dubbing_pipeline/api/routes_settings.py`
  - Add `notifications` subobject to user settings (e.g., enabled, ntfy_topic override, notify_on: done/failed).
  - Validate allowed fields; store in `UserSettingsStore`.
- `src/dubbing_pipeline/web/templates/settings.html`
  - Add per-user ntfy settings UI (topic + enable toggles; explain private ntfy requirement).
- `src/dubbing_pipeline/jobs/queue.py`
  - Read per-user settings; override topic or skip when user disabled.
  - Keep privacy redaction logic intact; continue to use `PUBLIC_BASE_URL` for deep links.
- `docs/notifications.md`
  - Document per-user topics and how they relate to server config.

### 4) Model readiness dashboard (installed/enabled + reasons)
Existing partials:
- Models UI shows disk + loaded cache + optional prewarm controls only.【F:src/dubbing_pipeline/web/templates/models.html†L1-L127】
- Runtime models API returns cache state + disk + downloads flag.【F:src/dubbing_pipeline/api/routes_runtime.py†L46-L96】
- Config flags exist for Whisper/TTS/diarization/demucs/TOS/egress.【F:config/public_config.py†L204-L287】

Plan (extend existing models endpoint/UI):
- `src/dubbing_pipeline/api/routes_runtime.py`
  - Add `features` section: whisper sizes, XTTS + TOS, diarization, demucs, GPU status.
  - For each feature, include `enabled`, `installed`, and `reason` (missing module, flag disabled, TOS not agreed).
- `src/dubbing_pipeline/runtime/model_manager.py` (or small helper)
  - Provide capability checks (import-only, no downloads).
- `src/dubbing_pipeline/web/templates/models.html`
  - Render feature table with status + reasons.
- `scripts/verify_model_manager.py`
  - Extend to assert presence of `features` structure.

### 5) E2E reliability gates (new)
Existing partials:
- Two-user concurrency, upload resume, and job recovery already exist.【F:scripts/e2e_concurrency_two_users.py†L9-L110】【F:scripts/e2e_upload_resume.py†L102-L207】【F:scripts/e2e_job_recovery.py†L26-L105】
- Cancel path exists but not mid-run; Redis fallback/redis-up verification exist separately.【F:scripts/e2e_smoke_web.py†L181-L204】【F:scripts/verify_queue_fallback.py†L19-L56】【F:scripts/verify_queue_redis.py†L119-L160】

Plan (minimal new scripts, skip optional deps):
- Add `scripts/e2e_cancel_midrun.py`
  - Start a job with a long-running fake stage; cancel during execution; verify job state transitions.
- Add `scripts/e2e_worker_restart_midrun.py`
  - Start a job, simulate queue worker restart, verify recovery + no data loss.
- Add `scripts/e2e_redis_flap.py`
  - Start with Redis down → fallback mode, then bring Redis back and verify queue mode flip without failing submissions.
- Ensure all new scripts skip gracefully if Redis/ffmpeg/optional deps missing (follow `verify_queue_redis.py` patterns).【F:scripts/verify_queue_redis.py†L119-L160】

### 6) Release hygiene (artifacts + CI “pro steps”)
Existing partials:
- Guardrail script already detects tracked artifacts.【F:scripts/check_no_tracked_artifacts.py†L7-L67】
- Release workflow already skips signing/attestation when keys are missing.【F:.github/workflows/ci-release.yml†L94-L145】

Plan:
- Review repo for tracked artifacts and remove via `scripts/cleanup_git_artifacts.*` if needed.
- Validate any “pro” CI steps (sign/attest) are guarded with key presence; keep current skip behavior.
- Add a lightweight `scripts/verify_release_hygiene.py` (optional) to run guardrail checks and report on dead code conflicts (no heavy deps).

## C) Minimal new endpoints/pages/scripts (summary)
- API: optionally `/api/jobs/{id}/timeline` (if not embedding in existing job detail payload).
- UI: update `upload_wizard.html`, `job_detail.html`, `models.html`, `settings.html` (no new pages).
- Scripts: `e2e_cancel_midrun.py`, `e2e_worker_restart_midrun.py`, `e2e_redis_flap.py`, optional `verify_release_hygiene.py`.

Stop condition for implementation phase: add verifiers for each P1 item and ensure they pass without requiring GPU or optional features.
