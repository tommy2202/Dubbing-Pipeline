# P0 Policy Enforcement Integration Map (pre-wiring)

Goal: remove placeholder risk and map where enforcement already happens before
wiring `security/policy_enforcement.py` into routes. This document is a short,
actionable map; see `docs/p0_policy_matrix.md` for the full route matrix.

## Current enforcement points

### Remote access
- Enforced by ASGI middleware:
  - `src/dubbing_pipeline/api/remote_access.py` (`RemoteAccessASGIMiddleware`)
  - `src/dubbing_pipeline/server.py` (`app.add_middleware(RemoteAccessASGIMiddleware)`)
- The policy decisions are in `decide_remote_access` (same module).

### Auth (identity + RBAC + CSRF)
- Dependencies:
  - `src/dubbing_pipeline/api/deps.py`
    - `current_identity` (session cookie, Bearer JWT, X-Api-Key)
    - `require_role` / `require_scope` (RBAC + CSRF for cookie sessions)
- Public allowlist for auth handlers:
  - `src/dubbing_pipeline/api/public_endpoints.py`
- UI routes typically perform manual checks in:
  - `src/dubbing_pipeline/web/routes_ui.py`

### Invite-only access
- Self-registration disabled:
  - `src/dubbing_pipeline/api/routes_auth.py` (`/auth/register`, `/auth/signup`)
- Invite redemption (public, token required):
  - `src/dubbing_pipeline/api/routes_invites.py` (`/api/invites/redeem`)
- Admin invite management:
  - `src/dubbing_pipeline/api/routes_admin.py` (`/api/admin/invites`)

### Visibility / ownership
- Centralized rules:
  - `src/dubbing_pipeline/security/visibility.py`
- Routed through access helpers:
  - `src/dubbing_pipeline/api/access.py`
    - `require_job_access`
    - `require_library_access`
    - `require_file_access`
    - `require_upload_access`
- Examples of usage:
  - `src/dubbing_pipeline/server.py` (`/video/{job}`, `/files/{path:path}`)
  - `src/dubbing_pipeline/web/routes/jobs_read.py`
  - `src/dubbing_pipeline/web/routes/jobs_files.py`
  - `src/dubbing_pipeline/web/routes/library.py`

### Quotas
- Centralized rules:
  - `src/dubbing_pipeline/security/quotas.py` (`QuotaEnforcer`)
- Current enforcement call sites:
  - Uploads: `src/dubbing_pipeline/web/routes/uploads.py`
  - Job submit/batch: `src/dubbing_pipeline/web/routes/jobs_submit.py`
  - Job resume/rerun: `src/dubbing_pipeline/web/routes/jobs_actions.py`,
    `src/dubbing_pipeline/web/routes/jobs_review.py`
  - Admin rerun: `src/dubbing_pipeline/web/routes/admin.py`

## Wiring plan for `security/policy_enforcement.py` (no behavior changes yet)

The plan below lists exactly where the policy enforcement wrappers should be
integrated. This prompt does not change endpoint behavior; it only prepares
the map.

### Remote access
- Keep the existing ASGI middleware as the single enforcement point.
- Do **not** add per-route dependencies to avoid double enforcement.
- If a non-ASGI entrypoint is added in the future, call
  `policy_enforcement.require_remote_access` at that entrypoint boundary.

### Auth (identity + RBAC)
- Leave `require_role` / `require_scope` usage as-is. No policy_enforcement
  wrapper is planned for auth; it remains in `api/deps.py`.

### Invite-only
- Wire `policy_enforcement.require_invite_only` **only** if/when registration
  is re-enabled:
  - `src/dubbing_pipeline/api/routes_auth.py` (`/auth/register`, `/auth/signup`)
- Optional: use it as a guard in `routes_invites.redeem_invite` to centralize
  future invite-only policy checks.

### Visibility / ownership
- Update the access helpers in `src/dubbing_pipeline/api/access.py` to call the
  policy wrapper (future change):
  - `require_job_access` -> `policy_enforcement.require_can_view_job`
  - Add/extend wrappers for `require_can_view_artifact` and
    `require_can_view_library_item` if needed for full coverage.
- This automatically covers all routes that use these access helpers, including:
  - `/api/jobs/*` read/detail/files/logs (routes under `web/routes/jobs_*.py`)
  - `/api/library/*` (routes under `api/routes_library.py`)
  - `/video/{job}` and `/files/{path:path}` in `server.py`

### Quotas
- Use the policy wrapper for upload-size checks where `bytes` is known:
  - `web/routes/uploads.py`
    - `POST /api/uploads/init`
    - `POST /api/uploads/{upload_id}/chunk`
    - `POST /api/uploads/{upload_id}/complete`
  - `web/routes/jobs_submit.py`
    - `POST /api/jobs`
    - `POST /api/jobs/batch`
- Keep direct `QuotaEnforcer` calls for checks not yet represented in the
  wrapper (jobs/day, concurrent jobs, processing minutes) until the wrapper
  is expanded in a later prompt.
