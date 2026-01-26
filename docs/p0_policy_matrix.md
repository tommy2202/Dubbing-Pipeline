# P0 Policy Enforcement Matrix (scan)

Generated from repo scan of the FastAPI app in `src/dubbing_pipeline/server.py`.

Notes:
- `auth_router` is mounted at both `/auth/*` and `/api/auth/*` (same handlers).
- `remote_access_middleware` is HTTP-only; WebSocket routes are **not** covered.
- Static mount: `/static` (StaticFiles).
- Test-only FastAPI apps under `scripts/*` are excluded (not mounted in server).

## Prompt 2 updates (invite-only enforcement)

- `/auth/register` and `/auth/signup` remain hard-disabled (404) under both `/auth/*` and `/api/auth/*`.
- Removed register/signup from the public endpoint allowlist to avoid accidental exposure.
- Added tests to assert no public signup and invalid invite redemption fails; invite creation stays admin-only.

## Prompt 3 updates (visibility enforcement)

- Centralized visibility checks in `src/dubbing_pipeline/security/visibility.py`.
- `require_job_access`, `require_library_access`, and `require_file_access` now delegate to the centralized guard.
- Added regression tests for private artifact leakage and guard invocation coverage.

## Required middleware/dependencies

- `remote_access_middleware` (`api/remote_access.py`), applied as HTTP middleware in `server.py`.
- `request_context_middleware` (`api/middleware.py`) for request_id context.
- `security_headers` + `log_requests` middleware (`server.py`).
- CORS middleware (configured origins + credential cookies).
- Auth dependencies: `current_identity`, `require_role`, `require_scope` (`api/deps.py`).
  - `current_identity` accepts **session cookie**, **Bearer JWT**, or **X-Api-Key**.
- Visibility/access helpers: `require_job_access`, `require_library_access`,
  `require_file_access`, `require_upload_access` (`api/access.py`).
- Quota/policy helpers: `resolve_user_quotas` + `get_limits` + `used_minutes_today`
  (`jobs/limits.py`), `evaluate_submission` (`jobs/policy.py`).

Auth column legend:
- **public** = no auth
- **session/bearer/api-key** = current_identity (cookie, Bearer JWT, X-Api-Key)
- **scope: X** / **role: Y** are enforced via `require_scope` / `require_role`

## Route matrix

### Core + static

| Method | Path | Source | Auth requirement | Invite-only gating | Visibility/ownership enforcement | Quota enforcement | Remote access gating |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GET | /health | `src/dubbing_pipeline/server.py:health` | public | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /healthz | `src/dubbing_pipeline/server.py:healthz` | public | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /metrics | `src/dubbing_pipeline/server.py:metrics` | public | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /readyz | `src/dubbing_pipeline/server.py:readyz` | public | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | / | `src/dubbing_pipeline/server.py:home` | session/bearer/api-key + role: viewer | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /login | `src/dubbing_pipeline/server.py:login_redirect` | public | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /video/{job} | `src/dubbing_pipeline/server.py:video` | session/bearer/api-key + scope: read:job | N/A | `require_file_access` (owner/admin; allow_shared_read=True) | None | `remote_access_middleware` (HTTP) |
| GET | /files/{path:path} | `src/dubbing_pipeline/server.py:files` | session/bearer/api-key + scope: read:job | N/A | `require_file_access` (owner/admin; allow_shared_read=True) | None | `remote_access_middleware` (HTTP) |
| MOUNT | /static/* | `src/dubbing_pipeline/server.py:StaticFiles` | public | N/A | N/A | None | `remote_access_middleware` (HTTP) |

### Auth + invites

| Method | Path | Source | Auth requirement | Invite-only gating | Visibility/ownership enforcement | Quota enforcement | Remote access gating |
| --- | --- | --- | --- | --- | --- | --- | --- |
| POST | /auth/login (also /api/auth/login) | `api/routes_auth.py:login` | public (username/password) | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| POST | /auth/refresh (also /api/auth/refresh) | `api/routes_auth.py:refresh` | refresh cookie + CSRF | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| POST | /auth/logout (also /api/auth/logout) | `api/routes_auth.py:logout` | refresh cookie + CSRF | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| POST | /auth/totp/setup (also /api/auth/totp/setup) | `api/routes_auth.py:totp_setup` | session/bearer/api-key; admin role check in handler | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| POST | /auth/totp/verify (also /api/auth/totp/verify) | `api/routes_auth.py:totp_verify` | session/bearer/api-key; admin role check in handler | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| POST | /auth/qr/init (also /api/auth/qr/init) | `api/routes_auth.py:qr_init` | session/bearer/api-key + role: admin | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| POST | /auth/qr/redeem (also /api/auth/qr/redeem) | `api/routes_auth.py:qr_redeem` | public (one-time QR code) | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /auth/sessions (also /api/auth/sessions) | `api/routes_auth.py:list_sessions` | session/bearer/api-key | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| POST | /auth/sessions/{device_id}/revoke (also /api/auth/sessions/{device_id}/revoke) | `api/routes_auth.py:revoke_session` | session/bearer/api-key + CSRF | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| POST | /auth/sessions/revoke_all (also /api/auth/sessions/revoke_all) | `api/routes_auth.py:revoke_all_sessions` | session/bearer/api-key + CSRF | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET,POST | /auth/register (also /api/auth/register) | `api/routes_auth.py:register_disabled` | public (disabled, 404) | invite-only enforced by disable | N/A | None | `remote_access_middleware` (HTTP) |
| GET,POST | /auth/signup (also /api/auth/signup) | `api/routes_auth.py:signup_disabled` | public (disabled, 404) | invite-only enforced by disable | N/A | None | `remote_access_middleware` (HTTP) |
| POST | /api/invites/redeem | `api/routes_invites.py:redeem_invite` | public | invite token required (creates user) | N/A | None | `remote_access_middleware` (HTTP) |

### Admin / ops / keys / system / settings / audit

| Method | Path | Source | Auth requirement | Invite-only gating | Visibility/ownership enforcement | Quota enforcement | Remote access gating |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GET | /api/admin/queue | `api/routes_admin.py:admin_queue` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| POST | /api/admin/jobs/{id}/priority | `api/routes_admin.py:admin_job_priority` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| POST | /api/admin/jobs/{id}/cancel | `api/routes_admin.py:admin_job_cancel` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| POST | /api/admin/users/{id}/quotas | `api/routes_admin.py:admin_user_quotas` | session/bearer/api-key + role: admin | N/A | admin-only | N/A (quota override) | `remote_access_middleware` (HTTP) |
| GET | /api/admin/reports | `api/routes_admin.py:admin_list_reports` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /api/admin/reports/summary | `api/routes_admin.py:admin_reports_summary` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| POST | /api/admin/reports/{id}/resolve | `api/routes_admin.py:admin_resolve_report` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /api/admin/quotas/{user_id} | `api/routes_admin.py:admin_get_user_quota` | session/bearer/api-key + role: admin | N/A | admin-only | N/A (quota view) | `remote_access_middleware` (HTTP) |
| POST | /api/admin/quotas/{user_id} | `api/routes_admin.py:admin_set_user_quota` | session/bearer/api-key + role: admin | N/A | admin-only | N/A (quota override) | `remote_access_middleware` (HTTP) |
| POST | /api/admin/jobs/{id}/visibility | `api/routes_admin.py:admin_job_visibility` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /api/admin/glossaries | `api/routes_admin.py:admin_list_glossaries` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| POST | /api/admin/glossaries | `api/routes_admin.py:admin_create_glossary` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| PUT | /api/admin/glossaries/{id} | `api/routes_admin.py:admin_update_glossary` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| DELETE | /api/admin/glossaries/{id} | `api/routes_admin.py:admin_delete_glossary` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /api/admin/pronunciation | `api/routes_admin.py:admin_list_pronunciation` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| POST | /api/admin/pronunciation | `api/routes_admin.py:admin_create_pronunciation` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| PUT | /api/admin/pronunciation/{id} | `api/routes_admin.py:admin_update_pronunciation` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| DELETE | /api/admin/pronunciation/{id} | `api/routes_admin.py:admin_delete_pronunciation` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /api/admin/metrics | `api/routes_admin.py:admin_metrics` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /api/admin/jobs/failures | `api/routes_admin.py:admin_job_failures` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /api/admin/voices/suggestions | `api/routes_admin.py:admin_list_voice_profile_suggestions` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| POST | /api/admin/voices/approve_merge | `api/routes_admin.py:admin_approve_voice_profile_merge` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /api/admin/invites | `api/routes_admin.py:admin_list_invites` | session/bearer/api-key + role: admin | N/A | admin-only | N/A (invite management) | `remote_access_middleware` (HTTP) |
| POST | /api/admin/invites | `api/routes_admin.py:admin_create_invite` | session/bearer/api-key + role: admin | N/A | admin-only | N/A (invite management) | `remote_access_middleware` (HTTP) |
| GET | /keys | `api/routes_keys.py:list_keys` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| POST | /keys | `api/routes_keys.py:create_key` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| POST | /keys/{key_id}/revoke | `api/routes_keys.py:revoke_key` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /api/runtime/state | `api/routes_runtime.py:runtime_state` | session/bearer/api-key + role: operator | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /api/runtime/queue | `api/routes_runtime.py:runtime_queue_status` | session/bearer/api-key + role: operator | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /api/runtime/models | `api/routes_runtime.py:runtime_models` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| POST | /api/runtime/models/prewarm | `api/routes_runtime.py:runtime_models_prewarm` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /api/system/readiness | `api/routes_system.py:system_readiness` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /api/system/security-posture | `api/routes_system.py:system_security_posture` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /api/settings | `api/routes_settings.py:get_settings_me` | session/bearer/api-key | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| PUT | /api/settings | `api/routes_settings.py:put_settings_me` | session/bearer/api-key + role: operator | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /api/admin/users/{user_id}/settings | `api/routes_settings.py:admin_get_user_settings` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| PUT | /api/admin/users/{user_id}/settings | `api/routes_settings.py:admin_put_user_settings` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /api/audit/recent | `api/routes_audit.py:audit_recent` | session/bearer/api-key | N/A | user-scoped in handler | None | `remote_access_middleware` (HTTP) |

### Library API (`/api/library/*`)

| Method | Path | Source | Auth requirement | Invite-only gating | Visibility/ownership enforcement | Quota enforcement | Remote access gating |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GET | /api/library/series | `api/routes_library.py:library_series` | session/bearer/api-key + scope: read:job | N/A | `require_library_access` (allow_shared_read=True) | None | `remote_access_middleware` (HTTP) |
| GET | /api/library/search | `api/routes_library.py:library_search` | session/bearer/api-key + scope: read:job | N/A | `require_library_access` (allow_shared_read=True) | None | `remote_access_middleware` (HTTP) |
| GET | /api/library/recent | `api/routes_library.py:library_recent` | session/bearer/api-key + scope: read:job | N/A | `require_library_access` (allow_shared_read=True) | None | `remote_access_middleware` (HTTP) |
| GET | /api/library/continue | `api/routes_library.py:library_continue` | session/bearer/api-key + scope: read:job | N/A | `require_library_access` (allow_shared_read=True) | None | `remote_access_middleware` (HTTP) |
| GET | /api/library/{series_slug}/seasons | `api/routes_library.py:library_seasons` | session/bearer/api-key + scope: read:job | N/A | `require_library_access` (allow_shared_read=True) | None | `remote_access_middleware` (HTTP) |
| GET | /api/library/{series_slug}/{season_number}/episodes | `api/routes_library.py:library_episodes` | session/bearer/api-key + scope: read:job | N/A | `require_library_access` (allow_shared_read=True) | None | `remote_access_middleware` (HTTP) |
| DELETE | /api/library/{series_slug}/{season_number}/{episode_number} | `api/routes_library.py:delete_library_episode` | session/bearer/api-key + scope: read:job | N/A | `require_library_access` + `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/library/{key}/admin_remove | `api/routes_library.py:library_admin_remove` | session/bearer/api-key + scope: read:job (admin check in handler) | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| POST | /api/library/{key}/unshare | `api/routes_library.py:library_unshare` | session/bearer/api-key + scope: read:job | N/A | `require_library_access` (allow_shared_read=True) + owner/admin check | None | `remote_access_middleware` (HTTP) |
| POST | /api/library/{key}/report | `api/routes_library.py:library_report` | session/bearer/api-key + scope: read:job | N/A | `require_library_access` (allow_shared_read=True) + visibility check for shared/public | None | `remote_access_middleware` (HTTP) |

### Jobs: list/read/actions

| Method | Path | Source | Auth requirement | Invite-only gating | Visibility/ownership enforcement | Quota enforcement | Remote access gating |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GET | /api/jobs | `web/routes/jobs_read.py:list_jobs` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` per job (owner/admin; no shared) | None | `remote_access_middleware` (HTTP) |
| GET | /api/project-profiles | `web/routes/jobs_read.py:list_project_profiles` | session/bearer/api-key + scope: read:job | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{id} | `web/routes/jobs_read.py:get_job` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| DELETE | /api/jobs/{id} | `web/routes/jobs_actions.py:delete_job` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/visibility | `web/routes/jobs_actions.py:set_job_visibility` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/cancel | `web/routes/jobs_actions.py:cancel_job` | session/bearer/api-key + scope: submit:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/pause | `web/routes/jobs_actions.py:pause_job` | session/bearer/api-key + scope: submit:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/resume | `web/routes/jobs_actions.py:resume_job` | session/bearer/api-key + scope: submit:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |

### Jobs: submission

| Method | Path | Source | Auth requirement | Invite-only gating | Visibility/ownership enforcement | Quota enforcement | Remote access gating |
| --- | --- | --- | --- | --- | --- | --- | --- |
| POST | /api/jobs | `web/routes/jobs_submit.py:create_job` | session/bearer/api-key + scope: submit:job | N/A | upload ownership enforced via `require_upload_access` when upload_id used | max_upload_bytes, max_storage_bytes, daily_processing_minutes, jobs/day cap, queued cap (policy) | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/batch | `web/routes/jobs_submit.py:create_jobs_batch` | session/bearer/api-key + scope: submit:job | N/A | N/A (new jobs) | max_upload_bytes, max_storage_bytes, jobs/day cap, queued cap (policy) | `remote_access_middleware` (HTTP) |

### Jobs: files/preview/logs/events

| Method | Path | Source | Auth requirement | Invite-only gating | Visibility/ownership enforcement | Quota enforcement | Remote access gating |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GET | /api/jobs/{id}/files | `web/routes/jobs_files.py:job_files` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (allow_shared_read=True) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{id}/outputs | `web/routes/jobs_files.py:job_outputs_alias` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (allow_shared_read=True) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{id}/preview/audio | `web/routes/jobs_files.py:job_preview_audio` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (allow_shared_read=True) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{id}/preview/lowres | `web/routes/jobs_files.py:job_preview_lowres` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (allow_shared_read=True) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{id}/stream/manifest | `web/routes/jobs_files.py:job_stream_manifest` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (allow_shared_read=True) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{id}/stream/chunks/{chunk_idx} | `web/routes/jobs_files.py:job_stream_chunk` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (allow_shared_read=True) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{id}/qrcode | `web/routes/jobs_files.py:job_qrcode` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (allow_shared_read=True) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{id}/timeline | `web/routes/jobs_logs.py:get_job_timeline` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{id}/logs/tail | `web/routes/jobs_logs.py:tail_logs` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{id}/logs | `web/routes/jobs_logs.py:logs_alias` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{id}/logs/stream | `web/routes/jobs_logs.py:stream_logs` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/events | `web/routes/jobs_events.py:jobs_events` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` per job (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /events/jobs/{id} | `web/routes/jobs_events.py:sse_job` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| WS | /ws/jobs/{id} | `web/routes/jobs_events.py:ws_job` | Bearer JWT / API key / session cookie (manual auth); optional legacy token | N/A | `require_job_access` (owner/admin) | None | **None (WebSocket; HTTP middleware not applied)** |

### Jobs: review / overrides

| Method | Path | Source | Auth requirement | Invite-only gating | Visibility/ownership enforcement | Quota enforcement | Remote access gating |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GET | /api/jobs/{id}/overrides | `web/routes/jobs_review.py:get_job_overrides` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{id}/overrides/music/effective | `web/routes/jobs_review.py:get_job_music_regions_effective` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| PUT | /api/jobs/{id}/overrides | `web/routes/jobs_review.py:put_job_overrides` | session/bearer/api-key + scope: edit:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/overrides/apply | `web/routes/jobs_review.py:apply_job_overrides` | session/bearer/api-key + scope: edit:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{id}/characters | `web/routes/jobs_review.py:get_job_characters` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| PUT | /api/jobs/{id}/characters | `web/routes/jobs_review.py:put_job_characters` | session/bearer/api-key + scope: edit:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{id}/segments | `web/routes/jobs_review.py:get_job_segments` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (allow_shared_read=True) | None | `remote_access_middleware` (HTTP) |
| PATCH | /api/jobs/{id}/segments/{segment_id} | `web/routes/jobs_review.py:patch_job_segment` | session/bearer/api-key + scope: edit:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/segments/{segment_id}/approve | `web/routes/jobs_review.py:post_job_segment_approve` | session/bearer/api-key + scope: edit:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/segments/{segment_id}/reject | `web/routes/jobs_review.py:post_job_segment_reject` | session/bearer/api-key + scope: edit:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/segments/rerun | `web/routes/jobs_review.py:post_job_segments_rerun` | session/bearer/api-key + scope: edit:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{id}/transcript | `web/routes/jobs_review.py:get_job_transcript` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| PUT | /api/jobs/{id}/transcript | `web/routes/jobs_review.py:put_job_transcript` | session/bearer/api-key + scope: edit:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/overrides/speaker | `web/routes/jobs_review.py:set_speaker_overrides_from_ui` | session/bearer/api-key + scope: edit:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/transcript/synthesize | `web/routes/jobs_review.py:synthesize_from_approved` | session/bearer/api-key + scope: edit:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{id}/review/segments | `web/routes/jobs_review.py:get_job_review_segments` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/review/segments/{segment_id}/helper | `web/routes/jobs_review.py:post_job_review_helper` | session/bearer/api-key + scope: edit:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/review/segments/{segment_id}/edit | `web/routes/jobs_review.py:post_job_review_edit` | session/bearer/api-key + scope: edit:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/review/segments/{segment_id}/regen | `web/routes/jobs_review.py:post_job_review_regen` | session/bearer/api-key + scope: edit:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/review/segments/{segment_id}/lock | `web/routes/jobs_review.py:post_job_review_lock` | session/bearer/api-key + scope: edit:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/review/segments/{segment_id}/unlock | `web/routes/jobs_review.py:post_job_review_unlock` | session/bearer/api-key + scope: edit:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{id}/review/segments/{segment_id}/audio | `web/routes/jobs_review.py:get_job_review_audio` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |

### Jobs: voice refs & mappings

| Method | Path | Source | Auth requirement | Invite-only gating | Visibility/ownership enforcement | Quota enforcement | Remote access gating |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GET | /api/jobs/{id}/voice_refs | `web/routes/jobs_voice_refs.py:get_job_voice_refs` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{id}/speakers | `web/routes/jobs_voice_refs.py:get_job_speakers` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{id}/voice_refs/{speaker_id}/audio | `web/routes/jobs_voice_refs.py:get_job_voice_ref_audio` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/voice_refs/{speaker_id}/override | `web/routes/jobs_voice_refs.py:post_job_voice_ref_override` | session/bearer/api-key + role: admin | N/A | `require_job_access` (admin/owner) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{job_id}/speaker-mapping | `web/routes/jobs_voice_refs.py:post_job_speaker_mapping` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` + `require_library_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/voice-mapping | `web/routes/jobs_voice_refs.py:post_job_voice_mapping` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /api/jobs/{job_id}/speaker-mapping | `web/routes/jobs_voice_refs.py:get_job_speaker_mapping` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |

### Uploads + server-local file picker

| Method | Path | Source | Auth requirement | Invite-only gating | Visibility/ownership enforcement | Quota enforcement | Remote access gating |
| --- | --- | --- | --- | --- | --- | --- | --- |
| POST | /api/uploads/init | `web/routes/uploads.py:uploads_init` | session/bearer/api-key + scope: submit:job | N/A | N/A | max_upload_bytes, max_storage_bytes | `remote_access_middleware` (HTTP) |
| GET | /api/uploads/{upload_id} | `web/routes/uploads.py:uploads_status` | session/bearer/api-key + scope: read:job | N/A | `require_upload_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /api/uploads/{upload_id}/status | `web/routes/uploads.py:uploads_status_minimal` | session/bearer/api-key + scope: read:job | N/A | `require_upload_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/uploads/{upload_id}/chunk | `web/routes/uploads.py:uploads_chunk` | session/bearer/api-key + scope: submit:job | N/A | `require_upload_access` (owner/admin) | upload total/size enforced via init; chunk size cap | `remote_access_middleware` (HTTP) |
| POST | /api/uploads/{upload_id}/complete | `web/routes/uploads.py:uploads_complete` | session/bearer/api-key + scope: submit:job | N/A | `require_upload_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /api/files | `web/routes/uploads.py:list_server_files` | session/bearer/api-key + scope: read:job | N/A | N/A (server-local Input dir listing) | None | `remote_access_middleware` (HTTP) |

### Admin/operator job utilities + presets/projects

| Method | Path | Source | Auth requirement | Invite-only gating | Visibility/ownership enforcement | Quota enforcement | Remote access gating |
| --- | --- | --- | --- | --- | --- | --- | --- |
| PUT | /api/jobs/{id}/tags | `web/routes/admin.py:set_job_tags` | session/bearer/api-key + role: operator | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/archive | `web/routes/admin.py:archive_job` | session/bearer/api-key + role: operator | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/unarchive | `web/routes/admin.py:unarchive_job` | session/bearer/api-key + role: operator | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/kill | `web/routes/admin.py:kill_job_admin` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| POST | /api/jobs/{id}/two_pass/rerun | `web/routes/admin.py:rerun_two_pass_admin` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /api/presets | `web/routes/admin.py:list_presets` | session/bearer/api-key + scope: read:job | N/A | owner-scoped (admin sees all) | None | `remote_access_middleware` (HTTP) |
| POST | /api/presets | `web/routes/admin.py:create_preset` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| DELETE | /api/presets/{id} | `web/routes/admin.py:delete_preset` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /api/projects | `web/routes/admin.py:list_projects` | session/bearer/api-key + scope: read:job | N/A | owner-scoped (admin sees all) | None | `remote_access_middleware` (HTTP) |
| POST | /api/projects | `web/routes/admin.py:create_project` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| DELETE | /api/projects/{id} | `web/routes/admin.py:delete_project` | session/bearer/api-key + role: admin | N/A | admin-only | None | `remote_access_middleware` (HTTP) |

### Library (voice store) web API

| Method | Path | Source | Auth requirement | Invite-only gating | Visibility/ownership enforcement | Quota enforcement | Remote access gating |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GET | /api/series/{series_slug}/characters | `web/routes/library.py:list_series_characters` | session/bearer/api-key + scope: read:job | N/A | `require_library_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /api/series/{series_slug}/characters/{character_slug}/audio | `web/routes/library.py:get_series_character_audio` | session/bearer/api-key + scope: read:job | N/A | `require_library_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/series/{series_slug}/characters | `web/routes/library.py:create_series_character` | session/bearer/api-key + scope: read:job | N/A | `require_library_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/series/{series_slug}/characters/{character_slug}/ref | `web/routes/library.py:upload_series_character_ref` | session/bearer/api-key + scope: read:job | N/A | `require_library_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/series/{series_slug}/characters/{character_slug}/promote-ref | `web/routes/library.py:promote_series_character_ref_from_job` | session/bearer/api-key + scope: read:job | N/A | `require_library_access` + `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| DELETE | /api/series/{series_slug}/characters/{character_slug} | `web/routes/library.py:delete_series_character` | session/bearer/api-key + scope: read:job | N/A | `require_library_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /api/voices/{series_slug}/{voice_id}/versions | `web/routes/library.py:get_voice_versions` | session/bearer/api-key + scope: read:job | N/A | `require_library_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/voices/{series_slug}/{voice_id}/rollback | `web/routes/library.py:rollback_voice_version` | session/bearer/api-key + scope: read:job | N/A | `require_library_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /api/voices/{profile_id}/suggestions | `web/routes/library.py:get_voice_profile_suggestions` | session/bearer/api-key + scope: read:job | N/A | `_require_voice_profile_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/voices/{profile_id}/accept_suggestion | `web/routes/library.py:accept_voice_profile_suggestion` | session/bearer/api-key + scope: read:job | N/A | `_require_voice_profile_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| POST | /api/voices/{profile_id}/consent | `web/routes/library.py:update_voice_profile_consent` | session/bearer/api-key + scope: read:job | N/A | `_require_voice_profile_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |

### WebRTC

| Method | Path | Source | Auth requirement | Invite-only gating | Visibility/ownership enforcement | Quota enforcement | Remote access gating |
| --- | --- | --- | --- | --- | --- | --- | --- |
| POST | /webrtc/offer | `web/routes_webrtc.py:webrtc_offer` | session/bearer/api-key + scope: read:job | N/A | `require_job_access` + `require_file_access` | None | `remote_access_middleware` (HTTP) |
| GET | /webrtc/demo | `web/routes_webrtc.py:webrtc_demo` | session/bearer/api-key + scope: read:job (manual) | N/A | `require_job_access` per job | None | `remote_access_middleware` (HTTP) |

### UI pages (`/ui/*`) + system UI

| Method | Path | Source | Auth requirement | Invite-only gating | Visibility/ownership enforcement | Quota enforcement | Remote access gating |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GET | /ui/health | `web/routes_ui.py:ui_health` | public | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /ui/login | `web/routes_ui.py:ui_login` | public (redirects if authenticated) | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /ui/dashboard | `web/routes_ui.py:ui_dashboard` | session/bearer/api-key (redirect if missing) | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /ui/library | `web/routes_ui.py:ui_library_series` | session/bearer/api-key (redirect if missing) | N/A | N/A (UI page only) | None | `remote_access_middleware` (HTTP) |
| GET | /ui/library/{series_slug} | `web/routes_ui.py:ui_library_seasons` | session/bearer/api-key (redirect if missing) | N/A | `require_library_access` (allow_shared_read=True) | None | `remote_access_middleware` (HTTP) |
| GET | /ui/library/{series_slug}/season/{season_number} | `web/routes_ui.py:ui_library_episodes` | session/bearer/api-key (redirect if missing) | N/A | `require_library_access` (allow_shared_read=True) | None | `remote_access_middleware` (HTTP) |
| GET | /ui/library/{series_slug}/season/{season_number}/episode/{episode_number} | `web/routes_ui.py:ui_library_episode_detail` | session/bearer/api-key (redirect if missing) | N/A | `require_library_access` (allow_shared_read=True) | None | `remote_access_middleware` (HTTP) |
| GET | /ui/voices/{series_slug}/{voice_id} | `web/routes_ui.py:ui_voice_detail` | session/bearer/api-key (redirect if missing) | N/A | `require_library_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /ui/partials/jobs_table | `web/routes_ui.py:ui_jobs_table` | session/bearer/api-key (redirect if missing) | N/A | `require_job_access` per job (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /ui/models | `web/routes_ui.py:ui_models` | session/bearer/api-key (redirect if missing) | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /ui/jobs/{job_id} | `web/routes_ui.py:ui_job_detail` | session/bearer/api-key (redirect if missing) | N/A | `require_job_access` (owner/admin) | None | `remote_access_middleware` (HTTP) |
| GET | /ui/upload | `web/routes_ui.py:ui_upload` | session/bearer/api-key (redirect if missing) | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /ui/presets | `web/routes_ui.py:ui_presets` | session/bearer/api-key (redirect if missing) | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /ui/projects | `web/routes_ui.py:ui_projects` | session/bearer/api-key (redirect if missing) | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /ui/settings | `web/routes_ui.py:ui_settings` | session/bearer/api-key (redirect if missing) | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /ui/settings/notifications | `web/routes_ui.py:ui_settings_notifications` | session/bearer/api-key (redirect if missing) | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /ui/admin/queue | `web/routes_ui.py:ui_admin_queue` | session/bearer/api-key + admin role (manual check) | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /ui/admin/dashboard | `web/routes_ui.py:ui_admin_dashboard` | session/bearer/api-key + admin role (manual check) | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /ui/admin/reports | `web/routes_ui.py:ui_admin_reports` | session/bearer/api-key + admin role (manual check) | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /ui/admin/glossaries | `web/routes_ui.py:ui_admin_glossaries` | session/bearer/api-key + admin role (manual check) | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /ui/admin/pronunciation | `web/routes_ui.py:ui_admin_pronunciation` | session/bearer/api-key + admin role (manual check) | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /ui/admin/voice-suggestions | `web/routes_ui.py:ui_admin_voice_suggestions` | session/bearer/api-key + admin role (manual check) | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /ui/admin/invites | `web/routes_ui.py:ui_admin_invites` | session/bearer/api-key + admin role (manual check) | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /ui/qr | `web/routes_ui.py:ui_qr_redeem` | public | N/A | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /invite/{token} | `web/routes_ui.py:ui_invite_redeem` | public | invite flow entrypoint | N/A | None | `remote_access_middleware` (HTTP) |
| GET | /system/readiness | `web/routes_system.py:ui_system_readiness` | session/bearer/api-key + admin role (manual check) | N/A | admin-only | None | `remote_access_middleware` (HTTP) |
| GET | /system/security | `web/routes_system.py:ui_system_security_posture` | session/bearer/api-key + admin role (manual check) | N/A | admin-only | None | `remote_access_middleware` (HTTP) |

## P0 fixes needed (by category)

### Invite-only
- None observed. Self-registration routes are disabled (`/auth/register`, `/auth/signup`), and user creation is gated by invite redemption (`/api/invites/redeem`) + admin invite creation.

### Visibility / ownership
- None observed. Artifact/library endpoints consistently use `require_job_access`, `require_library_access`, or `require_file_access`.

### Quotas
- None observed. Job submissions (`/api/jobs`, `/api/jobs/batch`) and uploads (`/api/uploads/init`) enforce upload size/storage and policy caps.

### Remote access
- **WebSocket `/ws/jobs/{id}` is not covered by `remote_access_middleware` (HTTP-only).**
  - P0 fix: add remote-access enforcement for WebSocket connections (e.g., ASGI middleware or explicit check in `ws_job`).
