# Privacy/Trusted Shared Library Guardrails Plan

This document records the canonical code locations (scan results) and the
planned file-level changes to implement guardrails (1,2,4,7,8,9). No code
changes are included here.

## A) Canonical locations (scan)

### Account creation / registration paths
- `src/dubbing_pipeline/server.py`
  - Admin bootstrap user creation using `ADMIN_USERNAME` / `ADMIN_PASSWORD`.
- `src/dubbing_pipeline/api/models.py`
  - `AuthStore` user table + `upsert_user`.
- `src/dubbing_pipeline/api/routes_auth.py`
  - Login/refresh/logout/QR; no signup/register routes present today.

### Job metadata / manifest schema
- `src/dubbing_pipeline/jobs/models.py`
  - `Job` dataclass, `Visibility` enum, and default handling in `from_dict`.
- `src/dubbing_pipeline/jobs/manifests.py`
  - Stage manifest schema and writer for `manifests/*.json`.
- `src/dubbing_pipeline/jobs/store.py`
  - `job_library` table schema + `_maybe_upsert_library_from_raw`.

### Library index / manifest writer
- `src/dubbing_pipeline/library/manifest.py`
  - Canonical `write_manifest` for `manifest.json`.
- `src/dubbing_pipeline/jobs/store.py`
  - Denormalized library index writer.
- `src/dubbing_pipeline/library/queries.py`
  - Library query layer used by API/UI.

### Quotas / rate limits / storage
- `src/dubbing_pipeline/jobs/limits.py`
  - Upload size, duration caps, per-day minutes helper.
- `src/dubbing_pipeline/jobs/policy.py`
  - Daily job caps + per-user queue caps.
- `src/dubbing_pipeline/utils/ratelimit.py`
  - RateLimiter (Redis-backed optional).
- `src/dubbing_pipeline/queue/redis_queue.py`, `queue/manager.py`
  - Per-user queue quotas stored in Redis.
- `src/dubbing_pipeline/web/routes/uploads.py`
  - Upload size enforcement.
- `src/dubbing_pipeline/web/routes/jobs_submit.py`
  - Job submission limits enforcement.
- `src/dubbing_pipeline/ops/storage.py`
  - Global disk free guard.
- `src/dubbing_pipeline/ops/retention.py`
  - Cleanup and bytes freed accounting.

### ntfy notification module
- `src/dubbing_pipeline/notify/ntfy.py`
- `src/dubbing_pipeline/notify/base.py`
- `config/public_config.py` + `config/secret_config.py`
  - ntfy settings + auth secret.

### Logging setup / redaction filter
- `src/dubbing_pipeline/utils/log.py`
  - Redaction filter + structlog config.
- `src/dubbing_pipeline/server.py`
  - `safe_log` request logging.
- `src/dubbing_pipeline/ops/audit.py`
  - Audit log redaction.

### Audit log mechanism
- `src/dubbing_pipeline/ops/audit.py`
- `src/dubbing_pipeline/api/middleware.py` (`audit_event`)
- `src/dubbing_pipeline/api/routes_audit.py` (reader)

## B) Guardrail implementation plan (per requirement)

### (1) Invite-only: no self signups
**Files to change/add**
- `config/public_config.py`
  - Add `SELF_SIGNUP_ENABLED` (default `false`).
- `src/dubbing_pipeline/api/routes_auth.py`
  - Add explicit `/auth/register` + `/auth/signup` handlers that return 403/404
    when `SELF_SIGNUP_ENABLED` is false. Emit `audit_event` for blocked attempts.
- `src/dubbing_pipeline/api/routes_admin.py`
  - Add admin-only user invite/create endpoint (uses AuthStore).
- `src/dubbing_pipeline/api/models.py`
  - Add `AuthStore.list_users()` / `create_user()` helpers (no duplicate SQL).
- Tests (optional)
  - Add regression test that `/auth/register` is blocked by default.

**Behavior**
- No unauthenticated/self registration.
- Only admin (or bootstrap via `ADMIN_USERNAME` / `ADMIN_PASSWORD`) can create users.
- All blocked attempts audited with coarse metadata only.

**Defaults**
- `SELF_SIGNUP_ENABLED = false` (new)

---

### (2) Per-job Private vs Shared toggle (default Private)
**Files to change/add**
- `src/dubbing_pipeline/web/routes/jobs_submit.py`
  - Parse `visibility` from request; accept `private|shared` and default `private`.
  - Set `Job.visibility` explicitly on create.
- `src/dubbing_pipeline/jobs/models.py`
  - Accept `shared` as an alias for `public` when parsing.
  - Keep default `private` for missing values.
- `src/dubbing_pipeline/jobs/store.py`
  - Normalize `visibility` values when indexing `job_library`.
- `src/dubbing_pipeline/library/queries.py`
  - Allow non-admins to see `visibility=shared` items; keep private owner-only.
- `src/dubbing_pipeline/api/access.py`
  - Allow read access when item visibility is shared/public.
- `src/dubbing_pipeline/library/manifest.py`
  - Persist visibility as `private|shared` in `manifest.json`.
- `src/dubbing_pipeline/web/routes/jobs_actions.py`
  - Add owner-scoped `POST /api/jobs/{id}/visibility` toggle endpoint.
- `src/dubbing_pipeline/api/routes_admin.py`
  - Accept `shared` alias in `admin_job_visibility`.
- `src/dubbing_pipeline/api/routes_settings.py`
  - Add default `visibility` in user settings; validate `private|shared`.
- `src/dubbing_pipeline/web/routes_ui.py`
  - Pass `visibility` default into upload wizard.
- Templates:
  - `templates/upload_wizard.html`
  - `templates/job_detail.html`
  - `templates/library_episode_detail.html`
  - Add toggle + shared/private badge, default private.

**Behavior**
- New jobs default to Private.
- Shared items appear to other users only when explicitly toggled.
- CSRF/RBAC/ownership checks remain intact.

**Defaults**
- Job visibility default: `private`.
- User settings default: `visibility = "private"` (new).

---

### (4) Quotas/throttles (upload size, jobs/day, concurrent jobs/user, storage/user)
**Files to change/add**
- `config/public_config.py`
  - Add `MAX_STORAGE_MB_PER_USER` (new).
- `src/dubbing_pipeline/jobs/limits.py`
  - Surface storage quota in limits helper (if desired).
- `src/dubbing_pipeline/jobs/policy.py`
  - Continue enforcing `DUBBING_DAILY_JOB_CAP` + per-user queue caps.
- `src/dubbing_pipeline/web/routes/uploads.py`
  - Enforce per-user storage quota before upload init/complete.
- `src/dubbing_pipeline/web/routes/jobs_submit.py`
  - Enforce per-user storage quota before job submit.
- `src/dubbing_pipeline/jobs/store.py`
  - Add `user_storage` table and helpers to track usage.
  - Store per-job `storage_bytes` in `job.runtime` or a dedicated column.
- `src/dubbing_pipeline/ops/retention.py`
  - Decrement usage on job artifact deletion.
- `src/dubbing_pipeline/queue/redis_queue.py`
  - Extend per-user quota payload if storage caps are centrally stored.

**Behavior**
- Upload size: enforced by `MAX_UPLOAD_MB`.
- Jobs/day: enforced by `DUBBING_DAILY_JOB_CAP` (count) and
  `DAILY_PROCESSING_MINUTES` (duration).
- Concurrent jobs/user: enforced by `DUBBING_MAX_ACTIVE_JOBS_PER_USER` and
  `DUBBING_MAX_QUEUED_JOBS_PER_USER`.
- Storage/user: enforce `MAX_STORAGE_MB_PER_USER` using per-user usage tracking.

**Defaults (current)**
- `MAX_UPLOAD_MB = 2048`
- `DUBBING_DAILY_JOB_CAP = 0` (disabled)
- `DAILY_PROCESSING_MINUTES = 240`
- `DUBBING_MAX_ACTIVE_JOBS_PER_USER = 1`
- `DUBBING_MAX_QUEUED_JOBS_PER_USER = 5`
- `MAX_CONCURRENT = 1`
- `MIN_FREE_GB = 10`

**New default proposal**
- `MAX_STORAGE_MB_PER_USER = 10240` (10GB). Adjust if product requires tighter bounds.

---

### (7) Access model
**Requirement**: Prefer Tailscale-only; Cloudflare Tunnel must require Access
Policy A allowlist/Access Group.

**Files to change/add**
- `config/public_config.py`
  - Change default `REMOTE_ACCESS_MODE` to `tailscale`.
  - Add `CLOUDFLARE_ACCESS_GROUP_ALLOWLIST` (group IDs/emails).
- `src/dubbing_pipeline/api/remote_access.py`
  - Enforce allowlist against Cloudflare Access JWT claims.
  - Fail closed when allowlist is configured and claims do not match.

**Behavior**
- Default remote access is Tailscale/LAN allowlist.
- In `cloudflare` mode:
  - Access JWT verification required (already).
  - Allowlist enforced (new).

**Defaults**
- `REMOTE_ACCESS_MODE = tailscale` (planned change from `off`)
- `TRUST_PROXY_HEADERS = false` (must be true for Cloudflare)
- `CLOUDFLARE_ACCESS_TEAM_DOMAIN = None`
- `CLOUDFLARE_ACCESS_AUD = None`
- `CLOUDFLARE_ACCESS_GROUP_ALLOWLIST = ""` (empty unless configured)

---

### (8) Admin quick-remove + user report/remove (ntfy)
**Files to change/add**
- `src/dubbing_pipeline/api/routes_admin.py`
  - Add admin quick-unshare endpoint (set visibility private + update manifest/index).
- `src/dubbing_pipeline/web/routes/jobs_actions.py`
  - Add owner-level unshare endpoint (non-admin).
- `src/dubbing_pipeline/api/routes_library.py`
  - Add user report/remove endpoint.
  - Report triggers admin ntfy + audit; remove hides item for that user.
- `src/dubbing_pipeline/jobs/store.py`
  - Add `library_reports` + `library_blocks` tables.
- `src/dubbing_pipeline/library/queries.py`
  - Exclude blocked items for a user.
- `src/dubbing_pipeline/notify/ntfy.py`
  - Reuse `notify()` with `NTFY_ADMIN_TOPIC`.
- Templates:
  - `templates/library_episodes.html`
  - `templates/library_episode_detail.html`
  - Add Report/Remove UI actions.

**Behavior**
- Admin can immediately unshare any shared item.
- User report/remove sends private ntfy alert to admins and hides the item locally.
- All actions audited with coarse metadata only.

**Defaults**
- `NTFY_ENABLED = false`
- `NTFY_NOTIFY_ADMIN = false`
- `NTFY_ADMIN_TOPIC = ""`

---

### (9) Minimize logging of content
**Files to change/add**
- `src/dubbing_pipeline/utils/log.py`
  - Expand redaction patterns and ensure transcript-like fields are summarized.
- `src/dubbing_pipeline/ops/audit.py`
  - Keep coarse audit logs only (already redacts; extend as needed).
- `src/dubbing_pipeline/server.py` + `src/dubbing_pipeline/api/middleware.py`
  - Ensure request logs always use `safe_log`.
- `src/dubbing_pipeline/stages/*` + `src/dubbing_pipeline/web/routes/*`
  - Replace any logs that include transcript content with metadata-only logs.
- Tests/scripts:
  - Extend `scripts/verify_no_secret_leaks.py` or add a new test to assert
    transcripts/cookies/tokens are never logged.

**Behavior**
- No transcript/subtitle content in logs by default.
- Tokens/cookies are redacted globally.
- Audit logs contain only coarse metadata.

**Defaults**
- `LOG_LEVEL = INFO`
- `LOG_MAX_BYTES = 5MB`
- `LOG_BACKUP_COUNT = 3`

## C) Migration notes (existing jobs/library)
- **Job visibility**:
  - `Job.from_dict` already defaults to `private` if missing.
  - Add a one-time backfill to set `job_library.visibility = "private"` when null/empty.
- **Manifests**:
  - Missing `visibility` in existing `manifest.json` treated as `private`.
  - New writes include visibility automatically.
- **Storage quota**:
  - Add a background scan to compute per-job `storage_bytes` from `Output/`.
  - Populate per-user totals without reading content.
- **Reports/blocks**:
  - New tables created with `IF NOT EXISTS`; no data migration needed.
- **Remote access**:
  - Config-only change; no data migration.
