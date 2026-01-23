# P2 Fix Map (polish/maintainability)

Scope: **P2 only** (no P0/P1 behavior changes).  
Goal: map exactly where to change/add code, what to split, and what tests to add.

---

## 1) Largest route files + responsibilities (for refactor A)

### Web routes
- **`src/dubbing_pipeline/web/routes_jobs.py` (~176 KB)**  
  Responsibilities: upload flow, job create/update/cancel, logs streaming, file serving, project/preset APIs, job review/overrides, artifacts, playback routes.
- **`src/dubbing_pipeline/web/routes_ui.py` (~19 KB)**  
  Responsibilities: UI page rendering + session gating.
- **`src/dubbing_pipeline/web/routes_webrtc.py` (~12 KB)**  
  Responsibilities: WebRTC auth/offer + media path resolution.

### API routes
- **`src/dubbing_pipeline/api/routes_auth.py` (~23 KB)**  
  Auth endpoints, login, sessions, CSRF, tokens.
- **`src/dubbing_pipeline/api/routes_settings.py` (~11 KB)**  
  User settings store + API.
- **`src/dubbing_pipeline/api/routes_library.py` (~7.6 KB)**  
  Library listing and delete endpoints.
- **`src/dubbing_pipeline/api/routes_system.py` (~18 KB)**  
  Readiness API (admin).

These are the primary candidates for P2 route splitting.

---

## 2) Canonical auth/ownership helpers (must remain centralized)

**Primary helpers (do not duplicate):**
- `src/dubbing_pipeline/api/access.py`
  - `require_job_access`
  - `require_upload_access`
  - `require_library_access`
  - `require_file_access`
- `src/dubbing_pipeline/api/deps.py`
  - `current_identity`
  - `require_scope`
  - `require_role`

**Rule:** any refactor must preserve these helpers as the **single source of truth** for authorization.

---

## P2 Scope Map (A–F)

### A) Safe route refactor (no behavior change)
**Goal:** split large route files without changing endpoints, auth, or response shapes.

**Primary files to split**
- `src/dubbing_pipeline/web/routes_jobs.py` into:
  - `web/routes_jobs_uploads.py` (upload init/chunk/complete/status)
  - `web/routes_jobs_logs.py` (tail/stream)
  - `web/routes_jobs_actions.py` (cancel/pause/resume/kill/delete)
  - `web/routes_jobs_files.py` (file serving/artifacts)
  - `web/routes_jobs_review.py` (review/overrides/voices)
  - `web/routes_jobs_admin.py` (admin-only job ops)
- `src/dubbing_pipeline/api/routes_auth.py` into:
  - `api/routes_auth_sessions.py`
  - `api/routes_auth_tokens.py`
  - `api/routes_auth_users.py`
- `src/dubbing_pipeline/api/routes_settings.py` into:
  - `api/routes_settings_user.py`
  - `api/routes_settings_admin.py`

**Boundary notes**
- Must keep router prefixes and dependencies identical.
- Must preserve `audit_event` and `safe_log` usage.

**Regression tests**
- Add `tests/test_routes_split_smoke.py` to assert all endpoints still resolve.
- Add a verifier `scripts/verify_routes_split.py` to import routers and call a few key paths with TestClient.

---

### B) Preview variants: audio-only + low-res for mobile
**Goal:** extra playback variants without changing existing ones.

**Files to touch**
- `src/dubbing_pipeline/stages/mixing.py` or `stages/export.py` (generate audio-only MP3/AAC)
- `src/dubbing_pipeline/stages/mkv_export.py` or new helper under `ops/export_variants.py`
- `src/dubbing_pipeline/web/routes_jobs.py` (expose new variants in `/api/jobs/{id}/files`)
- `src/dubbing_pipeline/web/templates/job_detail.html` (UI to play audio-only / low-res)
- `docs/WEB_MOBILE.md` (document new preview options)

**Artifacts**
- `Output/<job>/mobile/preview_low.mp4` (downscaled)
- `Output/<job>/audio/preview.mp3` (audio-only)

**Tests**
- `tests/test_preview_variants.py` (mock tiny media, assert files listed)
- `scripts/verify_preview_variants.py`

---

### C) Library browsing polish: search/sort/recent/continue
**Files to touch**
- `src/dubbing_pipeline/api/routes_library.py` (search query + sort param)
- `src/dubbing_pipeline/library/queries.py` (extend query methods)
- `src/dubbing_pipeline/web/templates/library_series.html`, `library_seasons.html`, `library_episodes.html`
  - add search input, sort dropdown, “recent” view
- `src/dubbing_pipeline/web/routes_ui.py` (pass parameters)

**Implementation notes**
- Continue-watching-ish: use last updated job per user (no new table; reuse job metadata).
- Ensure ownership filter stays enforced (`require_library_access` and query filters).

**Tests**
- `tests/test_library_search_sort.py`
- `scripts/verify_library_polish.py`

---

### D) Voice mapping review UI (pre-run review)
**Goal:** review diarization speakers + planned mappings before run.

**Files to touch**
- `src/dubbing_pipeline/web/templates/upload_wizard.html` (new “Review speakers” step)
- `src/dubbing_pipeline/web/routes_jobs.py` (endpoint to fetch predicted speaker list)
- `src/dubbing_pipeline/jobs/queue.py` (reuse existing diarization outputs; no new pipeline)
- `src/dubbing_pipeline/review/state.py` (if needs to store pre-run mapping)

**Notes**
- Must not trigger heavy models on UI fetch; use cached diarization or heuristic fallback.
- Reuse `voice_map` runtime fields; do not create new store.

**Tests**
- `tests/test_voice_mapping_review.py`
- `scripts/verify_voice_mapping_review.py`

---

### E) Voice drift + versioning (detect + rollback)
**Files to touch**
- `src/dubbing_pipeline/voice_store/embeddings.py` and/or `voices/registry.json` logic
- `src/dubbing_pipeline/voice_memory/` (existing embedding store)
- `src/dubbing_pipeline/web/templates/job_detail.html` (UI to compare/rollback)
- `src/dubbing_pipeline/api/routes_library.py` or new API under `api/routes_voice_versions.py`

**Notes**
- Record embedding hashes + timestamps.
- Allow rollback to previous ref; keep audit log.

**Tests**
- `tests/test_voice_versioning.py`
- `scripts/verify_voice_versioning.py`

---

### F) Optional scale path: Redis or Postgres on-ramp
**Goal:** document & scaffold optional storage without changing defaults.

**Files to touch**
- `src/dubbing_pipeline/queue/manager.py` (already supports Redis)
- `docs/SETUP.md` or new `docs/SCALE.md`
- `config/public_config.py` (document env vars; no new defaults)
- (Optional) add `scripts/verify_postgres_optional.py` to check env-only config

**Notes**
- Do not change default SQLite path.
- Postgres path should be **opt-in** and kept separate from v0/P1 defaults.

**Tests**
- `tests/test_optional_scale_path.py` (skips if env not set)
- `scripts/verify_queue_fallback.py` already covers fallback behavior.

---

## Regression test policy (P2)
- Route refactor: smoke tests for route registration + auth.
- Preview variants & library polish: tests + verifiers to avoid regressions.
- Voice drift/versioning: deterministic tests (no GPU).

---

## Logging & security checklist (P2)
- Keep `require_*_access` checks centralized.
- Ensure `request_id` is propagated in new endpoints.
- No secrets in UI or responses.
