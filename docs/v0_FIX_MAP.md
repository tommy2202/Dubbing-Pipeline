# v0 Hardening Fix Map

This map documents the canonical modules and the exact edit targets for the v0
security/privacy/reliability hardening deliverables.

## A) Canonical modules (scan results)

### Auth + scopes/roles
- `src/dubbing_pipeline/api/models.py` (Role, User, ApiKey, AuthStore)
- `src/dubbing_pipeline/api/deps.py` (Identity, `require_role`, `require_scope`, CSRF enforcement)
- `src/dubbing_pipeline/api/security.py` (JWT + CSRF helpers)
- `src/dubbing_pipeline/api/auth/refresh_tokens.py` (refresh token rotation)
- `src/dubbing_pipeline/api/routes_auth.py` (login/refresh/logout/session endpoints)
- `src/dubbing_pipeline/utils/security.py` (legacy api_token auth helper)
- `config/public_config.py` (auth toggles: `ENABLE_API_KEYS`, `ALLOW_LEGACY_TOKEN_LOGIN`, etc.)

### Job store + job routes
- `src/dubbing_pipeline/jobs/models.py` (Job model, `owner_id`, visibility)
- `src/dubbing_pipeline/jobs/store.py` (JobStore + SqliteDict tables)
- `src/dubbing_pipeline/jobs/queue.py` (job lifecycle + retention hooks)
- `src/dubbing_pipeline/web/routes_jobs.py` (canonical jobs API router)
- `src/dubbing_pipeline/api/routes_jobs.py` (back-compat import alias)
- `src/dubbing_pipeline/server.py` (router wiring)

### Library store + routes
- `src/dubbing_pipeline/jobs/store.py` (`job_library` table, owner_user_id)
- `src/dubbing_pipeline/library/queries.py` (object-level visibility filters)
- `src/dubbing_pipeline/library/paths.py` (Output/Library layout)
- `src/dubbing_pipeline/api/routes_library.py` (library API endpoints)
- `src/dubbing_pipeline/web/routes_ui.py` (library UI pages)

### Upload subsystem + storage
- `src/dubbing_pipeline/web/routes_jobs.py` (`/api/uploads/*`, direct form uploads)
- `src/dubbing_pipeline/jobs/store.py` (`uploads` SqliteDict table)
- `src/dubbing_pipeline/utils/paths.py` (uploads dir resolution)
- `src/dubbing_pipeline/runtime/lifecycle.py` (startup checks for uploads dir)
- `src/dubbing_pipeline/ops/retention.py` (purge old inputs)
- `src/dubbing_pipeline/security/crypto.py` (optional at-rest encryption for uploads)

### File serving / streaming / previews
- `src/dubbing_pipeline/server.py` (`/video`, `/files`, `_range_stream`)
- `src/dubbing_pipeline/web/routes_jobs.py` (`_file_range_response` for previews)
- `src/dubbing_pipeline/web/routes_ui.py` (UI routes referencing file previews)

### Client IP extraction
- `src/dubbing_pipeline/api/remote_access.py` (trusted proxy + forwarded parsing)
- `src/dubbing_pipeline/web/routes_jobs.py` (`_client_ip_for_limits`)
- `src/dubbing_pipeline/api/deps.py` (`_client_ip` for rate limits)
- `src/dubbing_pipeline/api/routes_auth.py` (`_client_ip` for auth limits)
- `src/dubbing_pipeline/utils/security.py` (`_client_ip` for legacy API token)

### Logging setup / middleware
- `src/dubbing_pipeline/utils/log.py` (structlog config + redaction + contextvars)
- `src/dubbing_pipeline/api/middleware.py` (`request_context_middleware`, audit wrapper)
- `src/dubbing_pipeline/server.py` (`log_requests` middleware)
- `src/dubbing_pipeline/ops/audit.py` (audit event writer)

### Retention / cleanup tasks
- `src/dubbing_pipeline/storage/retention.py` (per-job retention policy)
- `src/dubbing_pipeline/ops/retention.py` (global sweeper: inputs/logs)
- `src/dubbing_pipeline/jobs/queue.py` (retention after job completion)
- `src/dubbing_pipeline/web/routes_jobs.py` (retention knobs on submission)
- `src/dubbing_pipeline/runtime/lifecycle.py` (background tasks startup)
- `config/public_config.py` (retention settings)

## B) Deliverables mapping

### 1) Systematic object-level authorization for jobs/uploads/files/library (owner/admin)
**Files to edit/add**
- `src/dubbing_pipeline/web/routes_jobs.py`
  - Apply `_assert_job_owner_or_admin` to all job-specific endpoints
  - Enforce owner/admin for uploads, logs, previews, artifacts, SSE, and metadata
- `src/dubbing_pipeline/server.py`
  - `/video` and `/files` must validate job ownership (or public visibility) before serving
- `src/dubbing_pipeline/library/queries.py`
  - Ensure visibility filters are applied everywhere a library item is read
- `src/dubbing_pipeline/api/routes_library.py`
  - Ensure only owner/public items are returned (via queries)
- `src/dubbing_pipeline/jobs/store.py`
  - Ensure owner_id is persisted and indexed for legacy jobs

**Existing partials to extend**
- `_assert_job_owner_or_admin` in `web/routes_jobs.py`
- `library/queries._visibility_where[_with_view]`
- `job_library.owner_user_id` in `jobs/store.py`
- Upload records already store `owner_id` and are partially checked

**Tests/scripts to add**
- `tests/test_object_auth_jobs.py` (owner vs non-owner vs admin)
- `tests/test_upload_auth.py` (uploads status/chunk/complete ownership)
- `tests/test_library_visibility.py` (owner/private vs public)
- `tests/test_file_stream_auth.py` (`/video`, `/files` access control)
- `scripts/verify_object_auth.py` (fast smoke for cross-user access)

---

### 2) Safe file streaming (Range responses stream from disk)
**Files to edit/add**
- `src/dubbing_pipeline/web/routes_jobs.py`
  - Replace `_file_range_response` `read_bytes()` with disk streaming generator
- `src/dubbing_pipeline/server.py`
  - Reuse `_range_stream` helper for consistency (optional)
- (Optional) `src/dubbing_pipeline/utils/io.py`
  - Shared `range_stream(path, range_header)` helper

**Existing partials to extend**
- `_range_stream` in `server.py` already streams from disk
- `_file_range_response` in `web/routes_jobs.py` currently reads full file

**Tests/scripts to add**
- `tests/test_range_streaming.py` (Range + no-Range responses for previews)
- `scripts/verify_range_streaming.py` (static check or quick runtime check)

---

### 3) Trusted proxy handling (do not trust X-Forwarded-* unless peer is trusted)
**Files to edit/add**
- `src/dubbing_pipeline/api/remote_access.py`
  - Centralize the canonical "trusted proxy + forwarded" logic
- `src/dubbing_pipeline/web/routes_jobs.py`
  - Update `_client_ip_for_limits` to use shared helper
- `src/dubbing_pipeline/api/deps.py`, `src/dubbing_pipeline/api/routes_auth.py`,
  `src/dubbing_pipeline/utils/security.py`
  - Replace direct `request.client.host` usage with shared helper
- `src/dubbing_pipeline/server.py`
  - `_is_https_request` must verify peer is in `TRUSTED_PROXY_SUBNETS`
- `config/public_config.py`
  - Ensure `TRUSTED_PROXY_SUBNETS` is the single source of truth

**Existing partials to extend**
- `_extract_forwarded_ip` and trust logic in `api/remote_access.py`
- `_client_ip_for_limits` in `web/routes_jobs.py`

**Tests/scripts to add**
- `tests/test_trusted_proxy.py` (forwarded headers ignored unless peer is trusted)
- `scripts/verify_trusted_proxies.py` (static check for unsafe header use)

---

### 4) Concurrency safety (single-writer for SqliteDict/metadata writes; document worker model)
**Files to edit/add**
- `src/dubbing_pipeline/jobs/store.py`
  - Add process-wide lock (file lock) around SqliteDict writes
- `src/dubbing_pipeline/jobs/queue.py`
  - Document the single-writer/worker assumptions in code comments
- `docs/WORKER_MODEL.md` (new)
  - Document worker model and constraints (single writer, queue behavior)
- (Optional) `src/dubbing_pipeline/utils/locks.py` (shared lock helper)

**Existing partials to extend**
- `JobStore` already uses a thread lock and per-op SqliteDict open/close

**Tests/scripts to add**
- `tests/test_sqlitedict_single_writer.py` (multi-thread/process writes)
- `scripts/verify_single_writer.py` (smoke check for lock presence)

---

### 5) Privacy: retention sweeper ON by default + user-initiated delete
**Files to edit/add**
- `src/dubbing_pipeline/ops/retention.py`
  - Add periodic sweep loop entry point (or refactor for scheduler)
- `src/dubbing_pipeline/runtime/lifecycle.py`
  - Start retention sweeper at boot with a configurable interval
- `config/public_config.py`
  - Add `RETENTION_SWEEP_INTERVAL_SEC` (nonzero default to enable)
- `src/dubbing_pipeline/web/routes_jobs.py`
  - Add owner-initiated delete endpoint (owner/admin)
  - Ensure delete removes outputs + job metadata + upload records
- `src/dubbing_pipeline/jobs/store.py`
  - Helper to delete job + related metadata safely

**Existing partials to extend**
- `ops/retention.run_once` (global sweeper exists)
- Per-job retention in `storage/retention.py` and `jobs/queue.py`
- Admin-only job delete endpoint in `web/routes_jobs.py`

**Tests/scripts to add**
- `tests/test_retention_sweeper.py` (sweeper deletes old inputs/logs)
- `tests/test_job_delete_owner.py` (owner delete removes artifacts)
- `scripts/verify_retention.py` (existing; include in v0 gate)
- `scripts/verify_retention_sweeper.py` (new, periodic sweep smoke)

---

### 6) Logging: redact secrets/tokens; request_id correlation everywhere
**Files to edit/add**
- `src/dubbing_pipeline/utils/log.py`
  - Extend redaction patterns and ensure nested values are redacted
- `src/dubbing_pipeline/api/middleware.py`
  - Ensure request_id is set for all HTTP requests (already present)
- `src/dubbing_pipeline/server.py`
  - Ensure access logs include `request_id`, `user_id`, `job_id`, `upload_id` where relevant
- `src/dubbing_pipeline/ops/audit.py`
  - Ensure audit records include request_id everywhere

**Existing partials to extend**
- Redaction regexes + `_secret_literals()` in `utils/log.py`
- `request_context_middleware` already sets request_id

**Tests/scripts to add**
- `tests/test_log_redaction.py` (secret strings are masked)
- `tests/test_request_id.py` (header propagation + correlation)
- `scripts/verify_no_secret_leaks.py` (existing; include in v0 gate)
- `scripts/verify_request_id.py` (new, quick smoke)

---

### 7) Fresh-machine dev path (pytest with `pip -e .[dev]`)
**Files to edit/add**
- `docs/FRESH_MACHINE_SETUP.md`
  - Add explicit `pytest` command after `pip -e ".[dev]"`
- `docs/SETUP.md`
  - Document single-command dev install + `pytest` invocation

**Existing partials to extend**
- `docs/FRESH_MACHINE_SETUP.md` already uses `pip -e ".[dev]"`

**Tests/scripts to add**
- No new tests; doc-only change

---

### 8) `scripts/v0_gate.py` (run verifiers, fail if protections missing)
**Files to add**
- `scripts/v0_gate.py` (new gate script)

**Existing partials to extend**
- `scripts/polish_gate.py` (pattern for gate runner)
- Existing verifiers to include:
  - `scripts/verify_no_secret_leaks.py`
  - `scripts/verify_retention.py`
  - `scripts/security_smoke.py`
  - `scripts/verify_env.py`

**New verifiers to include in v0 gate**
- `scripts/verify_object_auth.py`
- `scripts/verify_range_streaming.py`
- `scripts/verify_trusted_proxies.py`
- `scripts/verify_single_writer.py`
- `scripts/verify_retention_sweeper.py`
- `scripts/verify_request_id.py`
