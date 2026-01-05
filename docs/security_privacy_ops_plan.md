## Security + Privacy + Ops + UX vNext — Phase 1 Plan (scan + design)

This document is the **Phase 1 repo-wide scan output** for the “Security + Privacy + Ops + UX vNext” upgrade.

Constraints honored by the design:
- **No parallel/duplicate systems**: extend the existing canonical v2 server, auth, jobs, uploads, storage, and ops modules only.
- **No accidental new public entry points**: remote access remains **opt-in** (`REMOTE_ACCESS_MODE=off` by default).
- **Defaults unchanged** unless it’s a clear security bug fix (new features are **off by default** / best-effort).
- Every future change (Phase 2+) must ship with **fallbacks**, **structured logging** (incl. `job_id`, `stage`, `user_id` where applicable), and **verification scripts/tests**.

---

## Current canonical modules map (paths + responsibility)

### App entrypoint + lifecycle
- `src/anime_v2/server.py`
  - Canonical FastAPI app, lifespan bootstrapping.
  - Registers routers:
    - Auth: `src/anime_v2/api/routes_auth.py` (`/api/auth/*`)
    - API keys: `src/anime_v2/api/routes_keys.py` (`/keys/*`)
    - Audit readback: `src/anime_v2/api/routes_audit.py` (`/api/audit/*`)
    - Settings: `src/anime_v2/api/routes_settings.py` (`/api/settings`)
    - Jobs/uploads/files: `src/anime_v2/web/routes_jobs.py` (`/api/*`)
    - UI pages: `src/anime_v2/web/routes_ui.py` (`/ui/*`)
    - WebRTC (optional): `src/anime_v2/web/routes_webrtc.py`
  - Adds middleware:
    - Remote access gating: `src/anime_v2/api/remote_access.py` (`remote_access_middleware`)
    - Request context + audit helper: `src/anime_v2/api/middleware.py`
    - Security headers + strict CORS (configured origins only)
  - Runs background systems:
    - Job queue: `src/anime_v2/jobs/queue.py` (in-proc workers)
    - Scheduler: `src/anime_v2/runtime/scheduler.py` (in-proc, caps/backpressure)
    - Periodic stale-workdir prune: `src/anime_v2/ops/storage.py`

### Configuration (canonical)
- `config/public_config.py`
  - Non-sensitive defaults; includes remote mode, upload sizing, retention policy knobs, mobile outputs toggles.
- `config/secret_config.py`
  - Sensitive settings loader (`.env.secrets`) with safe-to-commit defaults (dev-only secrets).
- `src/anime_v2/config.py` + `config/settings.py`
  - Canonical `get_settings()` aggregation (public + secret), helpers like `cors_origin_list()`.
- `.env.example`
  - Mixed “public + commented secrets”; already encourages `.env.secrets`.

### Authentication / authorization (canonical)
- `src/anime_v2/api/routes_auth.py`
  - Username/password login (argon2), JWT access tokens, rotating refresh tokens (server-side stored), cookie/session option.
  - CSRF for cookie/session flows (double-submit via `X-CSRF-Token` + `csrf` cookie).
  - TOTP support (optional; uses `pyotp` if installed):
    - `/api/auth/totp/setup`, `/api/auth/totp/verify`
- `src/anime_v2/api/security.py`
  - JWT encode/decode, CSRF token issue/verify, API key extraction.
- `src/anime_v2/api/models.py`
  - `AuthStore` (SQLite): users, api_keys, refresh_tokens.
  - Roles: `admin|operator|viewer`.
- `src/anime_v2/api/deps.py`
  - `current_identity()` supports:
    - API keys (`X-Api-Key` / bearer `dp_...`)
    - Bearer JWT access tokens
    - Signed session cookie (`session`)
    - Legacy `?token=` gated by `ALLOW_LEGACY_TOKEN_LOGIN=true` + private/loopback only
  - `require_role()` / `require_scope()` enforce RBAC + CSRF for cookie clients.

### Jobs / uploads / file picker / playback (canonical)
- `src/anime_v2/web/routes_jobs.py`
  - Chunked uploads: `/api/uploads/*` (size/ext/MIME allowlists, rate-limited).
  - Server file picker: `/api/files` (restricted to allowlisted base dirs).
  - Jobs: create/list/status/cancel/batch, logs, outputs/files list, artifacts.
  - QA + review/edit endpoints (segment text edits, regen, lock/unlock, audio preview).
  - QR code for job page: `/api/jobs/{id}/qrcode`.
- `src/anime_v2/jobs/store.py`
  - JobStore (SqliteDict): jobs table, idempotency keys, presets, projects, uploads.
- `src/anime_v2/jobs/queue.py`
  - In-proc workers running pipeline stages with watchdog timeouts and budgets; produces outputs and “mobile” artifacts.
- `src/anime_v2/stages/export.py`
  - Mobile artifacts:
    - MP4 (H.264/AAC): `export_mobile_mp4`
    - Optional HLS: `export_mobile_hls`
- `src/anime_v2/server.py`
  - Protected file serving:
    - `/files/{path:path}` (restricted to Output root via safe path join)
    - `/video/{job}` streaming with Range support

### UI templates (canonical)
- `src/anime_v2/web/templates/login.html`: session-cookie login UX (TOTP field present).
- `src/anime_v2/web/templates/dashboard.html`: jobs list/cards.
- `src/anime_v2/web/templates/job_detail.html`: tabs (Playback/Logs/QA/Review/Overrides), mobile playback links, review editor, overrides UI, QR for job page.
- `src/anime_v2/web/templates/upload_wizard.html`: chunked upload + server file picker + retention controls.
- `src/anime_v2/web/templates/settings.html`: per-user defaults + (currently) email/discord fields (not wired to notifications sending).

### Ops: logging, audit, retention, backups (canonical)
- `src/anime_v2/utils/log.py`
  - `structlog` JSON logs; includes request/user contextvars; **secret masking** (`_redact_str`) for JWTs, API keys, Bearer, and common `key=value` secrets.
- `src/anime_v2/ops/audit.py`
  - Append-only JSONL daily audit log `logs/audit-YYYYMMDD.log` with defensive redaction.
- `src/anime_v2/api/routes_audit.py`
  - `/api/audit/recent` returns per-user filtered recent audit events (current day file).
- `src/anime_v2/storage/retention.py`
  - Per-job retention policy: `full|balanced|minimal`, report written to `Output/<job>/analysis/retention_report.json`.
- `src/anime_v2/ops/retention.py`
  - Cross-job purges:
    - old uploads under `Input/uploads` (best-effort secure delete)
    - old logs under `logs/` and `Output/**/job.log`
- `src/anime_v2/ops/backup.py`
  - Metadata-only backups, including encrypted `data/**` (character store) and DBs.

### Remote access hardening (canonical)
- `src/anime_v2/api/remote_access.py`
  - `REMOTE_ACCESS_MODE=off|tailscale|cloudflare` with optional Cloudflare Access checks.
  - Trust forwarded headers only in Cloudflare mode and only from configured trusted proxy subnets.
- Docs + verifiers:
  - `docs/remote_access.md`
  - `scripts/verify_remote_mode.py`
  - `scripts/security_smoke.py`
  - `scripts/mobile_gate.py`

---

## Overlaps / conflicts (what to delete vs reroute)

These are candidates to **delete or keep as shims** in Phase 2+, to avoid parallel systems.

### Legacy pipeline / server implementations
- `main.py` and `src/anime_v1/*`
  - v1 UI/server + stages remain present and can confuse “what is canonical”.
  - **Plan**: keep for compatibility only, but ensure docs/scripts consistently point to v2; consider a “deprecated” banner and/or moving v1 under a clearly marked legacy package.

### Legacy auth mechanism (API_TOKEN / token-in-URL verifier)
- `src/anime_v2/utils/security.py`
  - Implements `verify_api_key()` that accepts `?token=` or `auth` cookie and compares to `API_TOKEN`.
  - This is **not part of the canonical v2 auth** (v2 uses JWT/session/API keys via `api/deps.py`).
  - **Plan**: audit imports/usages; if unused, remove. If used by legacy routes, reroute those routes to canonical auth dependencies or gate behind `ALLOW_LEGACY_TOKEN_LOGIN` + private IP only (same policy as current v2).

### Entrypoint duplication
- `src/anime_v2/web/app.py`
  - Currently a shim re-exporting `anime_v2.server.app` (good).
  - **Plan**: keep as compatibility shim only; no new logic should land here.

### Notifications fields not wired
- `src/anime_v2/api/routes_settings.py` includes `notifications.email` and `notifications.discord_webhook`
  - But no notification sender is plugged into the job lifecycle.
  - **Plan**: either wire them properly (with explicit opt-in + egress guard) or deprecate and replace with the requested `ntfy` private self-host integration.

---

## Threat model summary (LAN vs remote tunnel; what is exposed and what isn’t)

### Assets to protect
- **Credentials**: passwords, refresh tokens, session cookies, API keys, TOTP secrets, secrets in env.
- **Media**: uploaded source video/audio; intermediate audio stems/chunks; final outputs; review audio clips.
- **Metadata**: transcripts/subtitles, translations, QA reports, speaker embeddings/character store.
- **Operational data**: job logs, audit logs, settings, cache/model footprints.

### Trust boundaries & exposure
- **Default (LAN/dev)**:
  - Server binds `0.0.0.0`, but **remote gating is off** (`REMOTE_ACCESS_MODE=off`).
  - Protection relies on **auth** (JWT/session/API keys) and file-safety checks.
  - Primary risks: credential leakage, CSRF misconfig, path traversal in file serving, large uploads causing resource exhaustion.

- **Tailscale mode (recommended remote)**:
  - `REMOTE_ACCESS_MODE=tailscale` activates IP allowlisting for Tailscale/private ranges.
  - No public exposure required (no port-forwarding).
  - Primary risks: compromised tailnet device, weak admin password, leaked API key.

- **Cloudflare Tunnel mode (optional remote)**:
  - `REMOTE_ACCESS_MODE=cloudflare` + `TRUST_PROXY_HEADERS=1` (only when proxy is trusted).
  - Optional Cloudflare Access JWT validation.
  - Primary risks: misconfigured proxy trust (spoofed client IP), missing Access enforcement, overly broad CORS/origins.

### Attack surfaces (current)
- Upload endpoints (`/api/uploads/*`) and job creation (`/api/jobs`): resource exhaustion, malicious file formats.
- File serving (`/files/*`, `/video/*`): traversal bugs, Range abuse, unauthorized access.
- Auth endpoints (`/api/auth/*`): brute force, token fixation/replay, session theft.
- WebRTC (`/webrtc/*`): offer spam, TURN secret leaks, remote IP exposure.
- Settings/audit endpoints: privacy leaks via over-broad access.

---

## Per-feature design summary (A–M)

Each item below identifies:
- **Where it plugs in** (canonical modules only)
- **Data/schema impact**
- **Fallback behavior**
- **Logging/audit hooks**
- **Verification additions**

### A) Encryption at rest for sensitive artifacts (optional)

**Current state**
- Character store is already encrypted at rest:
  - `src/anime_v2/stages/character_store.py` uses AES-GCM with `CHAR_STORE_KEY` / `CHAR_STORE_KEY_FILE`.
- Job artifacts (uploads, outputs, logs) are plaintext on disk.

**Design**
- Add an **optional “job artifact encryption” layer** for a small, well-defined subset:
  - review state (`Output/<job>/review/state.json`)
  - review audio clips (`Output/<job>/review/*.wav` or `tts_clips`)
  - QA outputs if configured
  - uploaded source files in `Input/uploads` (optional; see B/F)
- Implement as a single canonical utility module:
  - `src/anime_v2/storage/crypto_at_rest.py` (new) with AES-GCM envelope format aligned with `CharacterStore` (magic/version/aad).
- Keying:
  - new secret `ARTIFACTS_KEY` / `ARTIFACTS_KEY_FILE` (32-byte base64), stored via `config/secret_config.py`.
  - Per-file random nonce; AAD includes `job_id`, artifact type, and schema version.

**Schema impact**
- None required; file formats become self-describing encrypted blobs.
- Optionally add a tiny `Output/<job>/analysis/encryption.json` marker (non-sensitive) for debugging.

**Fallback**
- Default **off**. If key missing:
  - do not encrypt; log a single structured warning at boot if user enabled it but key missing.

**Logging/audit**
- Audit events:
  - `security.crypto_at_rest.enabled`
  - `security.crypto_at_rest.encrypt_failed` (no plaintext leaks)

**Verification**
- `scripts/verify_crypto_at_rest.py`:
  - create synthetic job dir, encrypt/decrypt roundtrip, corrupted blob rejection.
  - ensure plaintext artifacts are not readable when encryption is enabled.

---

### B) Privacy mode + data minimization toggles + retention automation

**Current state**
- Per-job retention policy: `src/anime_v2/storage/retention.py` (`full|balanced|minimal`).
- Cross-job retention: `src/anime_v2/ops/retention.py` (old uploads + logs).
- UI exposes retention knobs on job submit (`upload_wizard.html`).

**Design**
- Introduce **Privacy Mode** as a unified policy layer that:
  - constrains what is stored (data minimization)
  - controls retention automation
  - controls whether “sensitive artifacts encryption” is required/encouraged
- Implement policy evaluation in one place:
  - `src/anime_v2/storage/privacy_policy.py` (new)
  - Consumes:
    - global config defaults (new fields in `config/public_config.py`)
    - per-job runtime overrides (existing job `runtime` dict)
    - per-user preferences (extend `UserSettingsStore`)

**Proposed toggles**
- `PRIVACY_MODE=off|balanced|minimal` (default `off` to preserve behavior)
- `MINIMIZE_LOGS=0/1` (default 0)
- `DELETE_UPLOAD_AFTER_INGEST=0/1` (default 0)
- `REDACT_SENSITIVE_TEXT=0/1` (default 0; for transcript content in logs/audit)
- `RETENTION_*` already exists; add:
  - `RETENTION_DAYS_JOBS_ARCHIVED` (optional)
  - `RETENTION_DAYS_JOBS_DELETED` (for secure delete workflow)

**Schema impact**
- Extend per-user settings JSON with:
  - `privacy.mode`, `privacy.auto_purge_days`, `privacy.encrypt_sensitive`
- Optionally add job metadata fields in JobStore:
  - `archived: bool`, `deleted_at: ts`, `tags: [str]` (ties into K)

**Fallback**
- Off by default; if toggles enabled but tooling missing (e.g., secure delete best-effort), degrade gracefully and log.

**Logging/audit**
- `privacy.policy_applied` (job_id, user_id, mode, deletions planned)
- `privacy.purge_*` events for inputs/logs/jobs (counts only; no filenames unless configured).

**Verification**
- Extend `scripts/verify_retention.py` and add `scripts/verify_privacy_mode.py`:
  - assert minimal policy deletes heavy intermediates and (optionally) uploads post-ingest.
  - ensure reports are produced and include bytes freed.

---

### C) Safer auth UX: QR login + session/device management + optional TOTP

**Current state**
- TOTP supported (`/api/auth/totp/*`) and enforced when enabled on the user.
- Refresh tokens are stored server-side (`AuthStore.refresh_tokens`) but currently lack “device/session” metadata.
- UI login is password-based with optional TOTP input.
- QR exists for job page linking (`/api/jobs/{id}/qrcode`) but not for login.

**Design**
1) **Device/session management**
   - Treat each refresh token (or each session cookie) as a **device session**.
   - Extend `refresh_tokens` schema with:
     - `device_id`, `device_name`, `user_agent_hash`, `created_ip`, `last_ip`
   - Add endpoints:
     - `GET /api/auth/sessions` (list own sessions)
     - `POST /api/auth/sessions/{device_id}/revoke` (revoke one)
     - `POST /api/auth/sessions/revoke_all` (revoke all but current)
   - UI: Settings → “Devices & Sessions”.

2) **QR login**
   - Add a short-lived “login intent”:
     - `POST /api/auth/qr/init` (unauthenticated; rate-limited) → returns `qr_id` and a URL to embed in QR.
     - `GET /api/auth/qr/{qr_id}/poll` (unauthenticated; long-poll/SSE) → when approved, returns tokens/cookies.
     - `POST /api/auth/qr/{qr_id}/approve` (authenticated on a trusted device) → marks as approved and issues a session for the scanning device.
   - UI flow:
     - Desktop/server page shows QR (login intent).
     - Mobile scans, opens “Approve login?” page which requires password/TOTP **or** uses an already-authenticated session on the phone.
   - Scope: intended for “phone login without typing” and “pair device” patterns.

**Fallback**
- Default off. QR login disabled unless `ENABLE_QR_LOGIN=1`.
- If `qrcode` dependency missing, fall back to showing a copyable link.

**Logging/audit**
- `auth.qr_init`, `auth.qr_approved`, `auth.qr_redeemed`, `auth.session_revoked`
- Include `user_id`, `request_id`, and redacted user-agent metadata.

**Verification**
- `scripts/verify_sessions.py`:
  - login, list sessions, revoke, ensure refresh rotation fails after revoke.
- `scripts/verify_qr_login.py`:
  - init intent, approve, poll redemption; assert rate limits.

---

### D) RBAC + scoped API keys

**Current state**
- Roles exist (`admin|operator|viewer`).
- Scope checks exist (`require_scope`), with default scopes: `read:job`, `submit:job`, `admin:*`.
- API keys exist (`/keys/*`) with stored scopes.

**Design**
- Expand scope model to support:
  - **resource-scoped** permissions:
    - `job:{job_id}:read`, `job:{job_id}:write`
    - `project:{project_id}:manage`
  - **capabilities**:
    - `audit:read`, `keys:manage`, `models:manage`, `library:manage`, `notifications:manage`
- Add a canonical scope helper:
  - `src/anime_v2/api/scopes.py` (new): parsing/matching helpers, stable registry of scope strings.
- Update `routes_keys.py` to create keys with:
  - explicit user ownership
  - optional expiry
  - optional job/project restriction fields

**Schema impact**
- Extend `api_keys` table:
  - `expires_at`, `name`, `created_ip`, `last_used_at`
  - `constraints_json` (resource scoping constraints)

**Fallback**
- Existing scope behavior remains; new scopes are additive.

**Logging/audit**
- `api_key.create`, `api_key.revoke` already exist; expand meta to include constraints and expiry.
- Add `api_key.used` (sampled) with prefix only (no secret).

**Verification**
- Extend `tests/test_rbac_csrf_ui.py` (already present) and add:
  - `tests/test_scoped_api_keys.py` (resource constraints enforced).

---

### E) Job isolation / resource limits (worker process + timeouts; optional container execution)

**Current state**
- JobQueue runs **in-process** and uses watchdog timeouts per stage.
- Scheduler enforces concurrency/backpressure.
- Disk free-space guard exists (`ensure_free_space`).

**Design**
- Add optional execution modes:
  1) **In-proc (current default)**: unchanged.
  2) **Worker subprocess mode (recommended vNext)**:
     - `JobQueue` spawns a child process per job (or per stage group) with:
       - hard wall-clock timeout
       - optional `resource.setrlimit` (CPU seconds, address space)
       - lower priority / nice (best-effort)
     - Child process reports progress back via:
       - append-only per-job log file (existing)
       - checkpoint updates (existing)
       - optional IPC via JSON status file in `Output/jobs/<job_id>/`
  3) **Optional container execution** (advanced, opt-in):
     - only if `ENABLE_CONTAINER_WORKER=1` and docker/podman available.
     - strict allowlist: only run an image specified by config; no arbitrary args.

**Schema impact**
- Add `job.runtime.worker_mode` and `job.runtime.limits` persisted for audit/debug.

**Fallback**
- If subprocess/container mode is enabled but unavailable (missing binaries/permissions), fall back to in-proc with a warning and an audit event.

**Logging/audit**
- `job.worker_spawned`, `job.worker_timeout`, `job.worker_exit` with exit codes only.

**Verification**
- `scripts/verify_worker_isolation.py`:
  - run a synthetic job with tiny media
  - assert timeout kills runaway stage (injectable test stage)

---

### F) File safety: ffprobe validation, allowlists, upload limits, safe storage

**Current state**
- Uploads enforce:
  - max size (`MAX_UPLOAD_MB`)
  - extension allowlist (`.mp4/.mkv/.mov/.webm/.m4v`)
  - MIME allowlist (best-effort)
  - optional `ffprobe` validation before job accept (job creation path uses ffprobe duration)
- File picker restricted to allowlisted base dirs; traversal prevented.
- File serving is rooted under Output (safe join in `server.py`).

**Design (tightening, without changing defaults)**
- Consolidate input validation into one canonical helper:
  - `src/anime_v2/storage/input_validation.py` (new)
  - Used by:
    - upload complete
    - job creation with server-local `video_path`
    - transcript/subtitle import (L)
- Expand `ffprobe` validation:
  - require at least one video stream
  - bound duration (`MAX_VIDEO_MIN`)
  - optionally reject suspicious codec combinations when in “mobile remote mode”
- Safe storage layout:
  - ensure uploads land in `Input/uploads/<upload_id>/<safe_name>`
  - store hash + ffprobe summary as metadata in JobStore uploads table

**Fallback**
- If ffprobe unavailable, keep current behavior but record “validation skipped” in audit.

**Logging/audit**
- `upload.validated` (size, ext, duration, streams_count)
- `upload.rejected` (reason code only)

**Verification**
- Extend `scripts/security_smoke.py` with:
  - malformed container rejection tests
  - strict traversal tests on `/files/*` and `/api/files`

---

### G) Secret hygiene: split env templates, startup checks, secret masking in logs

**Current state**
- Secrets are loaded via `config/secret_config.py` from `.env.secrets`.
- Logs redact JWTs, API keys, and common secret patterns (`utils/log.py`).
- Dev defaults for JWT/CSRF/session secrets exist (unsafe if used in remote).

**Design**
- Split templates:
  - `.env.public.example` (safe defaults, no secrets)
  - `.env.secrets.example` (only secret keys, commented + generation guidance)
- Add startup checks (best-effort; do not break local dev):
  - If `REMOTE_ACCESS_MODE != off`, require non-default secrets:
    - `JWT_SECRET`, `CSRF_SECRET`, `SESSION_SECRET`
  - If `COOKIE_SECURE=1`, ensure HTTPS detection is configured properly (already proxy-aware in Cloudflare mode).
- Extend log redaction patterns to cover:
  - TURN credentials (`turn_password`, `turn_username`)
  - `CLOUDFLARE_TUNNEL_TOKEN` (if present in env)
  - ntfy tokens (I)

**Fallback**
- Checks emit warnings + audit event `security.startup_weak_secrets` rather than blocking boot unless an explicit `ENFORCE_STRONG_SECRETS=1` is set.

**Verification**
- `scripts/verify_startup_checks.py`:
  - simulate remote mode with default secrets and assert warnings/audit events emitted.

---

### H) Security audit logging (separate from pipeline logs)

**Current state**
- `ops/audit.py` already provides a separate daily JSONL audit log, with API readback (`/api/audit/recent`).

**Design**
- Expand audit taxonomy and ensure **coverage** for security-relevant actions:
  - Auth: login success/failure (including TOTP required), refresh rotate, logout, session revoke, QR login lifecycle.
  - API keys: create/revoke/use (sampled).
  - Remote access: allow/deny decisions (already partially logged in remote access middleware).
  - File handling: upload init/complete, validation result, job create/cancel/delete/archive.
  - Privacy/retention: retention applied, purge actions, secure delete attempts.
- Make audit “security-grade”:
  - stable event schemas (documented)
  - strict redaction (already defensive)
  - add a config option to **mirror audit to stdout** for centralized collectors (optional)

**Fallback**
- Audit is best-effort: failures must not break requests; they should be logged once and suppressed.

**Verification**
- Extend `tests/test_audit_recent.py` (already present) to assert new event types appear and are filtered by user.

---

### I) Notifications on job completion (private ntfy self-host)

**Current state**
- User settings store has email/discord placeholders, but no sender hooked into job completion.

**Design**
- Add a single notifications plugin:
  - `src/anime_v2/ops/notify.py` (new) with `send_ntfy(...)`.
- Config:
  - public: `NOTIFY_ON_JOB_DONE=0/1` (default 0)
  - secret: `NTFY_BASE_URL`, `NTFY_TOPIC`, optional `NTFY_TOKEN`
  - per-user opt-in stored in `UserSettingsStore` (`notifications.ntfy_enabled`, `notifications.ntfy_topic_override`).
- Hook point:
  - `jobs/queue.py` after job transitions to DONE/FAILED/CANCELED.
  - Must respect `OFFLINE_MODE`/`ALLOW_EGRESS` via `egress_guard()`.

**Fallback**
- If ntfy not configured or egress denied: no-op with a single warning (rate-limited).

**Verification**
- `scripts/verify_notifications.py`:
  - monkeypatch HTTP client / run against a local mock server; assert payload shape and redaction.

---

### J) Mobile playback auto-selection (MP4/HLS vs master; Open-in-VLC)

**Current state**
- Mobile MP4 is produced by default (`MOBILE_OUTPUTS=1`), HLS optional (`MOBILE_HLS=0`).
- UI already prioritizes mobile artifacts and provides VLC links.

**Design**
- Improve selection logic without changing outputs:
  - If HLS enabled and user agent indicates iOS Safari, default to HLS.
  - Else prefer `mobile.mp4`.
  - Fall back to master MKV/MP4 if mobile artifacts missing.
- Add “copy direct link” affordances and ensure Range headers are correct for all.

**Fallback**
- If user-agent parsing fails: keep current behavior (mobile MP4 first).

**Verification**
- Extend `scripts/verify_mobile_outputs.py` to assert UI/API selection ordering and HLS content-type/paths.

---

### K) Library management (search/tags/recent/archive/delete)

**Current state**
- Jobs list exists (basic search by id/video_path; status filter).
- No tags, archive, delete; artifacts remain on disk until retention/prune.

**Design**
- Extend Job metadata (JobStore job record) with:
  - `title` (optional), `tags: [str]`, `archived: bool`, `deleted_at`, `notes`
- New endpoints (scope-protected):
  - `GET /api/library/jobs` (search + filters)
  - `POST /api/jobs/{id}/archive` / `/unarchive`
  - `DELETE /api/jobs/{id}` (soft delete by default; hard delete behind `ENABLE_HARD_DELETE=1`)
- Delete semantics:
  - soft delete hides from UI by default; retention job cleans up later
  - hard delete performs best-effort secure delete on sensitive artifacts and removes JobStore record

**Fallback**
- Defaults unchanged: archive/delete UI hidden unless enabled by config and role (`operator/admin`).

**Verification**
- `tests/test_library_management.py`:
  - tag, archive, list filters, soft delete hides, hard delete removes files within Output root only.

---

### L) External subtitles/transcripts import (skip ASR/translation where possible)

**Current state**
- Pipeline stages exist for transcription/translation; subtitles utilities exist (`anime_v2/subs/*`).
- No explicit import endpoints in v2.

**Design**
- Add import endpoints:
  - `POST /api/jobs/{id}/import/subtitles` (accept `.srt` / `.vtt`)
  - `POST /api/jobs/{id}/import/translated_json` (accept `translated.json` shape)
  - `POST /api/jobs/{id}/import/diarization_json` (optional)
- Store imports under `Output/<job>/imports/` and update job runtime to indicate “stage skip plan”.
- Modify `jobs/queue.py` stage orchestration:
  - if imported subtitles/translations present and validated, mark transcription/translation stages as done (checkpoint + stage manifest) and proceed.

**Fallback**
- Off by default (`ENABLE_IMPORTS=0`); when enabled, imports are optional and validated; invalid imports are rejected without affecting normal job processing.

**Verification**
- `scripts/verify_imports_skip.py`:
  - synthetic media + injected srt/translated.json; assert job completes without running whisper/translation (via checkpoint fields).

---

### M) Model management screen (cache status, pre-download, disk usage)

**Current state**
- `src/anime_v2/runtime/model_manager.py` provides in-memory cache and prewarm logging.
- No API/UI surface for model cache state or disk usage.

**Design**
- Add runtime API routes:
  - `GET /api/models/status` (cache entries, refcounts, last_used; plus disk usage for HF/Torch/TTS dirs)
  - `POST /api/models/prewarm` (admin-only; triggers `ModelManager.prewarm()` best-effort)
  - `POST /api/models/evict` (admin-only; best-effort eviction)
- UI page:
  - `/ui/models` (admin/operator) shows cache entries and disk usage, with “prewarm” button.
- Respect egress guard:
  - prewarm should not download if `OFFLINE_MODE=1` or `ALLOW_EGRESS=0`.

**Fallback**
- If optional dependencies missing (whisper/TTS), API still returns status and shows “not installed”.

**Verification**
- `tests/test_model_management_api.py`:
  - status endpoint returns disk usage keys and does not error without optional deps.

---

## Implementation notes (Phase 2+ guardrails)

- **No new public entrypoints**:
  - All new routes must be under existing routers and protected by `require_role`/`require_scope`.
  - QR login init/poll endpoints are unauthenticated but must be heavily rate-limited and short-lived.
- **Structured logging**:
  - Use `utils/log.py` logger with `request_id`/`user_id` context; include `job_id` and `stage` fields where applicable.
- **Audit logging**:
  - Use `api/middleware.audit_event()` for request-linked security events.
- **Backward compatibility**:
  - Keep `ALLOW_LEGACY_TOKEN_LOGIN=false` default; do not expand legacy token acceptance.
- **Remote safety**:
  - Any feature that could increase exposure (imports, delete, model management) must be role/scoped and remain disabled unless configured.

