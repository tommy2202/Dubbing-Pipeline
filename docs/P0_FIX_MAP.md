# P0 Fix Map

Scope: P0 items only. Extend canonical modules; no duplicate subsystems.

## Canonical modules (do not duplicate)
- Auth + RBAC + deps enforcement:
  - `src/dubbing_pipeline/api/deps.py` (Identity, require_role/scope, CSRF gating)
  - `src/dubbing_pipeline/api/models.py` (Role/User/AuthStore)
  - `src/dubbing_pipeline/api/security.py` (JWT + CSRF)
  - `src/dubbing_pipeline/api/routes_auth.py` (login/refresh/logout)
- Upload endpoints + chunk storage:
  - `src/dubbing_pipeline/web/routes_jobs.py` (`/api/uploads/*`, `_safe_filename`, chunk handling)
  - `src/dubbing_pipeline/jobs/store.py` (upload records)
  - `config/public_config.py` (upload limits)
- File serving (outputs/downloads):
  - `src/dubbing_pipeline/server.py` (`/files/*`, `/video/*`)
  - `src/dubbing_pipeline/web/routes_jobs.py` (`/api/jobs/{id}/files`, `/api/jobs/{id}/stream/*`)
  - `src/dubbing_pipeline/library/paths.py` (output roots)
- Job queue submit/status/cancel:
  - `src/dubbing_pipeline/web/routes_jobs.py` (submit, status, cancel, SSE, WS)
  - `src/dubbing_pipeline/jobs/policy.py` (submission/dispatch policy)
  - `src/dubbing_pipeline/runtime/scheduler.py` (global caps + backpressure)
  - `src/dubbing_pipeline/jobs/queue.py` (worker execution)
  - `src/dubbing_pipeline/queue/manager.py` + `queue/redis_queue.py` (L2 queue + quotas)
- Library endpoints:
  - `src/dubbing_pipeline/api/routes_library.py`
  - `src/dubbing_pipeline/library/queries.py`
  - `src/dubbing_pipeline/library/paths.py`
- Config/env handling:
  - `config/public_config.py`, `config/secret_config.py`, `config/settings.py`
  - `src/dubbing_pipeline/config.py` (shim)
- Logging + audit:
  - `src/dubbing_pipeline/utils/log.py`
  - `src/dubbing_pipeline/api/middleware.py` (request_id + audit_event)
  - `src/dubbing_pipeline/ops/audit.py`, `src/dubbing_pipeline/api/routes_audit.py`
- Middleware (CORS/CSRF/rate limit):
  - `src/dubbing_pipeline/server.py` (CORS + security headers + request_context)
  - `src/dubbing_pipeline/api/security.py` (verify_csrf)
  - `src/dubbing_pipeline/api/deps.py` (rate_limit helper)
  - `src/dubbing_pipeline/utils/ratelimit.py` (RateLimiter)
  - `src/dubbing_pipeline/api/remote_access.py` (IP allowlist)

## No duplicate paths confirmed
- `src/dubbing_pipeline/api/routes_jobs.py` is a re-export of `web/routes_jobs.py`; do not add parallel routers.
- `src/dubbing_pipeline/web/app.py` re-exports `server.app`; keep `server.py` canonical.
- `src/dubbing_pipeline/utils/security.py` is legacy API-token auth; do not reintroduce for new endpoints.
- `src/dubbing_pipeline/utils/config.py` is a shim; use `config/*` for settings changes.

---

## P0-1) Object-level access control (jobs/uploads/outputs/library) + tests

### Existing partial coverage
- Upload endpoints already check `owner_id` in `/api/uploads/*`.
- Library queries enforce owner/public visibility via `library/queries.py`.
- `_assert_job_owner_or_admin` exists in `web/routes_jobs.py` but is only used for voice refs.

### Files to modify
- `src/dubbing_pipeline/web/routes_jobs.py`
- `src/dubbing_pipeline/server.py`
- `src/dubbing_pipeline/web/routes_webrtc.py`
- `src/dubbing_pipeline/library/queries.py` (only if gaps found)
- `src/dubbing_pipeline/jobs/store.py` or `src/dubbing_pipeline/library/paths.py` (helper to map output path -> job)

### Exact checks to add
- Add a single helper (e.g., `_assert_job_access(job, ident, allow_public: bool)`) in `web/routes_jobs.py`.
  - Allow `admin` always.
  - Allow owner by `job.owner_id`.
  - Allow `public` only when endpoint is a read-only output endpoint.
- Apply the helper to all job endpoints that currently only check scopes:
  - `/api/jobs` (filter list to owner/public).
  - `/api/jobs/{id}` (detail).
  - `/api/jobs/{id}/cancel|pause|resume` (owner/admin only).
  - `/api/jobs/{id}/logs*`, `/api/jobs/{id}/files`, `/api/jobs/{id}/outputs`,
    `/api/jobs/{id}/stream/*`, `/api/jobs/events`, `/ws/jobs/{id}`.
- In `server.py`, enforce object-level access on `/files/*` and `/video/*`:
  - Resolve output path to a job_id (prefer `JobStore` mapping via output paths).
  - Use the same access helper (owner/admin/public) before streaming.
- In `routes_webrtc.py`, enforce job ownership before allowing `/webrtc/offer`.

### Tests / scripts to add
- New `tests/test_object_access_control.py`:
  - Create User A and User B + two jobs with different owners.
  - Assert User A cannot: list B jobs, get B job detail, stream B outputs/logs, connect SSE/WS.
  - Assert public visibility allows read-only access to outputs.
  - Assert upload status/chunk/complete reject other user upload_id.
- New `scripts/verify_object_access.py` (non-GPU) and add to `scripts/security_mobile_gate.py`.

---

## P0-2) Upload hardening (path traversal, filename sanitization, chunk integrity, size caps)

### Existing partial coverage
- `_safe_filename` and uploads dir checks prevent obvious traversal.
- Global `max_upload_mb` enforced at `/api/uploads/init`.
- Per-chunk SHA-256 integrity check exists.

### Files to modify
- `src/dubbing_pipeline/web/routes_jobs.py`
- `src/dubbing_pipeline/jobs/store.py`
- `config/public_config.py` + `config/settings.py`
- `scripts/security_file_smoke.py`

### Exact checks to add
- Enforce strict chunk ordering/integrity:
  - Require `offset == index * chunk_bytes`.
  - Prevent overlap with prior chunks; reject if `(offset, size)` overlaps any stored chunk.
  - On complete, verify all expected indices exist and total bytes match exactly.
- Strengthen filename sanitization:
  - Disallow leading dots, double extensions (`.mp4.exe`), and empty stems.
- Add per-user caps:
  - New settings: `MAX_UPLOAD_MB_PER_USER`, `MAX_UPLOADS_INFLIGHT_PER_USER`.
  - Before `uploads_init`, compute in-flight bytes/count for owner and enforce caps.

### Tests / scripts to add
- New `tests/test_upload_hardening.py`:
  - Reject bad offset/index mismatches and overlaps.
  - Reject per-user total bytes/count over cap.
  - Verify sanitized filename behavior.
- Extend `scripts/security_file_smoke.py` to include chunk ordering rejection.
- Add `scripts/verify_upload_hardening.py` if coverage exceeds the smoke script; wire into
  `scripts/security_mobile_gate.py`.

---

## P0-3) Resource abuse protections (global/per-user caps, queue limits, disk quota)

### Existing partial coverage
- `runtime/scheduler.py` enforces global concurrency + backpressure.
- `jobs/policy.py` enforces per-user queued caps and daily caps.
- `queue/redis_queue.py` supports per-user quotas (L2).
- `ops/storage.ensure_free_space` enforces global low-disk guard.

### Files to modify
- `src/dubbing_pipeline/jobs/policy.py`
- `src/dubbing_pipeline/runtime/scheduler.py`
- `src/dubbing_pipeline/queue/redis_queue.py`
- `src/dubbing_pipeline/queue/fallback_local_queue.py`
- `src/dubbing_pipeline/jobs/queue.py`
- `src/dubbing_pipeline/ops/storage.py`
- `config/public_config.py`
- `src/dubbing_pipeline/api/routes_admin.py`

### Exact checks to add
- Enforce per-user running caps at dispatch in both L2 and fallback queues.
- Add global cross-instance concurrency counters in Redis (not just high-mode).
- Add per-user disk quota (uploads + outputs):
  - Track bytes per user (new table in `jobs/store.py`).
  - Enforce at upload init/complete and at job submission/dispatch.
- Add low-disk refusal at job dispatch (not just submission), and per-user quota refusal.
- Add admin endpoints to read per-user usage (reuse `routes_admin.py`).

### Tests / scripts to add
- New `tests/test_storage_quota.py` (per-user disk limit).
- Extend `tests/test_job_limits.py` for per-user running caps and queue limits.
- Extend `scripts/verify_queue_limits.py` to cover per-user disk quota and dispatch caps.

---

## P0-4) Rate limiting verification (login/refresh/upload/job/status/SSE/WebRTC)

### Existing partial coverage
- Rate limits already exist for login, refresh, upload init/chunk/complete, job submit, WebRTC offer.

### Files to modify
- `src/dubbing_pipeline/web/routes_jobs.py`
- `src/dubbing_pipeline/api/routes_auth.py`
- `src/dubbing_pipeline/api/deps.py`
- `src/dubbing_pipeline/web/routes_webrtc.py`

### Exact checks to add
- Add rate limits for:
  - `/api/uploads/{upload_id}` status.
  - `/api/jobs` list, `/api/jobs/{id}` detail.
  - `/api/jobs/events` (SSE connect attempts).
  - `/api/jobs/{id}/logs/tail` and `/api/jobs/{id}/logs/stream`.
  - `/ws/jobs/{id}` websocket handshake.
- Use per-user + per-IP buckets; include request_id in logs for rate-limit denies.

### Tests / scripts to add
- New `tests/test_rate_limits.py` to hit endpoints until 429.
- Extend `scripts/security_smoke.py` to include refresh, status polling, SSE connect, WS handshake.

---

## P0-5) Secrets + production safety gates (refuse insecure defaults; never log secrets)

### Existing partial coverage
- `config/settings._validate_secrets` warns; strict mode only via `STRICT_SECRETS=1`.
- `utils/log.py` + `ops/audit.py` redact secrets.
- `scripts/verify_no_secret_leaks.py` exists.

### Files to modify
- `config/settings.py`
- `config/secret_config.py`
- `src/dubbing_pipeline/server.py`
- `.env.example`, `.env.secrets.example`
- `docs/SECURITY.md`

### Exact checks to add
- Define production mode (e.g., `ENV=production` or `REMOTE_ACCESS_MODE!=off` or `PUBLIC_BASE_URL` set).
- In production mode, hard-fail on insecure defaults without requiring `STRICT_SECRETS`.
- Expand redaction patterns to cover URL credentials and cookies if any logging still leaks them.
- Ensure all audit events use `audit_event` so request_id is included.

### Tests / scripts to add
- New `tests/test_secret_gates.py` for production-mode boot failure on defaults.
- Extend `scripts/verify_no_secret_leaks.py` to run in production mode.
- Add `scripts/verify_secrets_gate.py` and wire into `polish_gate.py`.

---

## P0-6) CORS/CSRF correctness (explicit allowlist in prod, cookie flags)

### Existing partial coverage
- Strict CORS allowlist configured in `server.py` using `cors_origins`.
- CSRF enforced in `require_role`/`require_scope` for cookie sessions.

### Files to modify
- `config/settings.py`
- `config/public_config.py`
- `src/dubbing_pipeline/server.py`
- `src/dubbing_pipeline/api/security.py`
- `docs/SECURITY.md`

### Exact checks to add
- In production mode:
  - Require non-empty `CORS_ORIGINS` (no wildcard).
  - Require `COOKIE_SECURE=1`.
- Verify cookies set correct flags:
  - `refresh` and `session`: `HttpOnly`, `Secure`, `SameSite=Lax` (or `None` if cross-site is required).
  - `csrf`: `Secure`, `SameSite=Lax`, not `HttpOnly`.
- Document exact production defaults and failure modes.

### Tests / scripts to add
- Extend `tests/test_rbac_csrf_ui.py` to assert cookie flags in production mode.
- Extend `scripts/security_smoke.py` to assert CORS allowlist enforcement.

---

## P0-7) Audit logging completeness (auth/job/download/admin; request_id)

### Existing partial coverage
- Auth events logged in `routes_auth.py`.
- Job submit/delete logged in `routes_jobs.py`.
- Admin job actions logged in `routes_admin.py` (some events).

### Files to modify
- `src/dubbing_pipeline/api/middleware.py`
- `src/dubbing_pipeline/ops/audit.py`
- `src/dubbing_pipeline/web/routes_jobs.py`
- `src/dubbing_pipeline/server.py`
- `src/dubbing_pipeline/api/routes_admin.py`

### Exact checks to add
- Add audit events for:
  - Job cancel, pause, resume (user actions).
  - File downloads (`/files/*`, `/video/*`, `/api/jobs/{id}/files`).
  - Admin queue views and quota changes (ensure request_id).
- Always include request_id + user_id in audit_event meta.

### Tests / scripts to add
- Extend `tests/test_audit_recent.py` to verify new events and request_id presence.
- Add `scripts/verify_audit_completeness.py` and wire into `security_mobile_gate.py`.

---

## P0-8) Shutdown lifecycle clean (no CancelledError noise; add verify script + gate)

### Existing partial coverage
- `runtime/lifecycle.stop_all` stops queue backend, scheduler, queue, tasks.
- `scripts/verify_shutdown_clean.py` exists and is in `polish_gate.py`.

### Files to modify
- `src/dubbing_pipeline/runtime/lifecycle.py`
- `src/dubbing_pipeline/queue/manager.py`
- `src/dubbing_pipeline/queue/redis_queue.py`
- `src/dubbing_pipeline/web/routes_jobs.py` (SSE)
- `src/dubbing_pipeline/web/routes_webrtc.py`
- `scripts/verify_shutdown_clean.py`
- `scripts/polish_gate.py`, `scripts/security_mobile_gate.py`

### Exact checks to add
- Suppress `asyncio.CancelledError` noise for background tasks and SSE generators.
- Ensure WebRTC idle tasks and peers are closed cleanly on shutdown.
- Add an explicit verify script (if `verify_shutdown_clean.py` is insufficient) and
  wire into `security_mobile_gate.py` (non-GPU).

### Tests / scripts to add
- New `tests/test_shutdown_clean.py` to validate clean TestClient shutdown.
- Add `scripts/verify_shutdown_no_cancelled.py` if needed and add to gate.

---

## P0-9) Dependency pin consistency (local + docker + docs)

### Existing partial coverage
- `docker/constraints.txt` pins versions; `docs/DEPENDENCY_POLICY.md` states it is canonical.

### Files to modify
- `pyproject.toml`
- `docker/constraints.txt`
- `docker/Dockerfile`, `docker/Dockerfile.cuda`
- `docs/DEPENDENCY_POLICY.md`, `docs/SETUP.md`, `docs/FRESH_MACHINE_SETUP.md`,
  `docs/TROUBLESHOOTING.md`, `docs/CLEAN_SETUP_GUIDE.txt`
- `scripts/verify_dependency_resolve.py` (or add new verify script)

### Exact checks to add
- Ensure local install instructions always use `-c docker/constraints.txt`.
- Keep `pyproject.toml` bounds compatible with pins (especially FastAPI/Starlette/sse-starlette).
- Add a verify script that diffs pins vs bounds and fails on mismatches.

### Tests / scripts to add
- Add `scripts/verify_dependency_pins.py` and wire into `polish_gate.py`.

