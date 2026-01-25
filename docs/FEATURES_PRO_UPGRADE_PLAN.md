# Features Pro Upgrade Plan (implementation plan only)

This document is a single implementation plan for:

1) Review/QA workflow UI: approve, tweak, rerun segments
2) Pronunciation/terminology tools: deterministic glossaries as fallback
3) "Track clone" stable per-speaker cloning across episodes/seasons
4) Monitoring + admin controls
5) Licensing/consent metadata for stored voices (friends-only safe)

Constraints honored:
- No duplicate code. Extend existing modules/helpers.
- No placeholder/wireframe UI. Implement real working screens + endpoints.
- Keep existing behavior unless explicitly upgraded.
- Add fallbacks and structured logging everywhere.
- Integrate with existing auth, library grouping, queue backend, manifest writer.

No code is added in this step. This is a design-only plan referencing current files.

---

## 0) Inventory of existing components (Task A)

### 0.1 Transcripts and segments
- Transcript editor and storage:
  - `src/dubbing_pipeline/web/routes/jobs_review.py` (`GET/PUT /api/jobs/{id}/transcript`)
  - `src/dubbing_pipeline/web/routes/jobs_common.py` (`transcript_store.json`, versions)
  - UI: `src/dubbing_pipeline/web/templates/partials/transcript_editor.html`
- Review segments and audio:
  - `src/dubbing_pipeline/review/state.py`, `src/dubbing_pipeline/review/ops.py`
  - `GET /api/jobs/{id}/review/segments` + edit/regen/lock/unlock/audio endpoints in `jobs_review.py`
- QA outputs:
  - `src/dubbing_pipeline/qa/scoring.py` writes `Output/<job>/qa/summary.json`, `segment_scores.jsonl`, `top_issues.md`
  - UI already reads QA summary/top issues via `/api/jobs/{id}/files` in `job_detail.html`

### 0.2 Job rerun/resume
- Resume/checkpoint:
  - `src/dubbing_pipeline/jobs/checkpoint.py`
  - `src/dubbing_pipeline/jobs/manifests.py` (stage manifests + resume)
- Job actions:
  - `src/dubbing_pipeline/web/routes/jobs_actions.py` (pause/resume/cancel/delete)
  - `src/dubbing_pipeline/web/routes/admin.py` (admin rerun pass2, kill)
- Queue backend:
  - `src/dubbing_pipeline/queue/manager.py` (AutoQueueBackend)
  - `src/dubbing_pipeline/queue/redis_queue.py`, `fallback_local_queue.py`

### 0.3 Voice persistence and embeddings
- Series-scoped character voices:
  - `src/dubbing_pipeline/voice_store/store.py`
  - `src/dubbing_pipeline/voice_store/embeddings.py`
  - DB tables in `src/dubbing_pipeline/jobs/store.py`: `character_voice`, `speaker_mapping`
- Voice memory (Tier-2A):
  - `src/dubbing_pipeline/voice_memory/store.py`
  - `src/dubbing_pipeline/voice_memory/embeddings.py`
- Speaker refs extraction:
  - `src/dubbing_pipeline/voice_refs/extract_refs.py` (writes `analysis/voice_refs/manifest.json`)
- TTS selection logic:
  - `src/dubbing_pipeline/stages/tts.py` (per-speaker refs, voice_store, fallbacks)

### 0.4 Library grouping (series/season/episode)
- Job model fields:
  - `src/dubbing_pipeline/jobs/models.py` (`series_title`, `series_slug`, `season_number`, `episode_number`)
- DB index:
  - `src/dubbing_pipeline/jobs/store.py` (`job_library` table)
- Library API/UI:
  - `src/dubbing_pipeline/api/routes_library.py`
  - `src/dubbing_pipeline/library/queries.py`
  - UI routes: `src/dubbing_pipeline/web/routes_ui.py` (`/ui/library/...`)

### 0.5 Admin routes and UI pages
- Admin API:
  - `src/dubbing_pipeline/api/routes_admin.py` (queue snapshot, job priority, quotas, reports, invites)
- Admin UI:
  - `/ui/admin/queue`, `/ui/admin/reports`, `/ui/admin/invites` in `routes_ui.py`
  - Templates: `admin_queue.html`, `admin_reports.html`, `admin_invites.html`
- System readiness/security:
  - `src/dubbing_pipeline/api/routes_system.py`
  - UI: `src/dubbing_pipeline/web/routes_system.py`, templates `system_readiness.html`, `system_security_posture.html`

### 0.6 Monitoring/metrics
- Prometheus metrics:
  - `src/dubbing_pipeline/ops/metrics.py`
  - `/metrics` endpoint in `src/dubbing_pipeline/server.py`
- Scheduler state:
  - `src/dubbing_pipeline/runtime/scheduler.py` (`state()`, queue stats)

---

## 1) Review/QA workflow UI (feature 1)

### 1.1 Goal
Provide an integrated workflow to:
- Review QA issues
- Approve/tweak segment text
- Regenerate audio per segment
- Rerun only approved/selected segments through TTS + mix

All without duplicating existing review/transcript logic.

### 1.2 Data model (reuse existing stores)
- Use existing file stores:
  - `Output/<job>/review/state.json` (segment status, chosen_text, audio_path_current)
  - `Output/<job>/transcript_store.json` (approved flags, tgt_text overrides, flags)
  - `Output/<job>/qa/*.jsonl` (QA signals)
- Add small, additive fields:
  - `review/state.json`:
    - `approved` (bool) per segment (mirror transcript store)
    - `qa_status` (info|warn|fail|ok) per segment (cached for UI)
  - `transcript_store.json`:
    - already has `approved` and `flags`; keep canonical approval here

No new SQL tables are required for QA workflow.

### 1.3 Exact endpoints to implement (Task C)
Reuse existing endpoints where possible, add minimal new endpoints:

Existing (already in repo, keep behavior):
- `GET /api/jobs/{id}/review/segments`
- `POST /api/jobs/{id}/review/segments/{segment_id}/edit`
- `POST /api/jobs/{id}/review/segments/{segment_id}/regen`
- `POST /api/jobs/{id}/review/segments/{segment_id}/lock`
- `POST /api/jobs/{id}/review/segments/{segment_id}/unlock`
- `GET /api/jobs/{id}/review/segments/{segment_id}/audio`
- `GET /api/jobs/{id}/transcript`
- `PUT /api/jobs/{id}/transcript`
- `POST /api/jobs/{id}/transcript/synthesize` (resynth from approved)

New endpoints (all must use existing auth helpers and audit logging):
- `GET /api/jobs/{id}/qa/summary`
  - Reads `Output/<job>/qa/summary.json` (fallback to `enabled=false`).
- `GET /api/jobs/{id}/qa/segments`
  - Parses `qa/segment_scores.jsonl` with filters: `severity`, `min_score`, `limit`.
  - Returns segment-level QA with segment_id, issues, suggested action.
- `POST /api/jobs/{id}/qa/run`
  - Enqueue QA scoring on the queue backend (non-blocking).
  - Writes to job runtime `qa_requested_at`.
- `POST /api/jobs/{id}/review/segments/approve`
  - Body: `{updates:[{segment_id, approved:true|false}]}`
  - Writes through `transcript_store` as the canonical approval store.
- `POST /api/jobs/{id}/review/synthesize`
  - Body: `{mode:"approved"|"selected", segment_ids?:[...] }`
  - Sets a resynth request in job runtime, queues job via `submit_job_or_503`.
  - Implemented as a thin wrapper around the existing resynth flow in `jobs/queue.py`.

All endpoints must use:
- `require_scope("read:job")` for reads
- `require_scope("edit:job")` for edits/approve/synthesize
- `audit_event` for each write action

### 1.4 UI changes (real screens, no placeholders)
Keep the single Job Detail page and extend the existing Review tab:
- Page: `/ui/jobs/{job_id}` (existing)
- Add QA filter panel:
  - Filter segments by severity (fail/warn/info)
  - Search by text/speaker
  - Show QA summary and counts
- Add approval toggle in review cards:
  - Writes to `/api/jobs/{id}/review/segments/approve`
- Add "Rerun approved" and "Rerun selected" buttons:
  - Calls `/api/jobs/{id}/review/synthesize`
- Keep "regen" and "lock/unlock" per segment as-is

No new UI route is necessary; only extend `job_detail.html` and the existing Alpine component.

### 1.5 Queue integration
- Use `submit_job_or_503` and existing queue backend logic.
- Job runtime marker:
  - `job.runtime.resynth = {type: "approved"|"selected", segment_ids, transcript_version, review_version, requested_at}`
- `jobs/queue.py` should detect `resynth` and run a minimal TTS+mix pass.

---

## 2) Pronunciation and terminology tools (feature 2)

### 2.1 Glossary and pronunciation format (Task D)
Use the existing style guide format, extend it without duplication:
- File format: YAML or JSON (already supported in `text/style_guide.py`)
- Example (version 1, ASCII-safe):

```
version: 1
project: "series_slug_or_project"
glossary_terms:
  - source: "Kobayashi"
    target: "Kobayashi"
    case_sensitive: false
pronunciation_lexicon:
  - term: "Kobayashi"
    tts: "Ko bye ah she"
    locale: "en"
    case_sensitive: false
```

Notes:
- `glossary_terms` stays canonical for translation enforcement.
- `pronunciation_lexicon` is applied only for TTS input (does not alter stored transcript).

### 2.2 Storage
Minimum new persistence:
- SQL table in jobs.db:
  - `series_glossary(series_slug TEXT PRIMARY KEY, data_json TEXT, updated_at REAL, updated_by TEXT)`
- File mirror (optional, best-effort) for offline/backup:
  - `Output/_state/glossaries/<series_slug>.json`

Existing file-based guides (kept working):
- `projects/<project>/style_guide.yaml` or `.json`
- `translation._read_glossary` TSV paths remain supported

### 2.3 Deterministic application
Translation (post-translate):
- Use `text/style_guide.apply_style_guide(...)` (already deterministic).
- When MT glossary mismatch occurs, apply deterministic replacement as fallback and log:
  - `logger.info("glossary_fallback_applied", ...)`

TTS pronunciation:
- Apply lexicon replacements to a `tts_text` field before synth:
  - `line["text_pre_pronunciation"]` stored in `tts_manifest.json`
  - `line["tts_text"]` used for synthesis
- Never mutate `translated.json` or `transcript_store.json` directly.

---

## 3) Track clone (stable per-speaker cloning) (feature 3)

### 3.1 Workflow (Task E)
Pipeline (reuse existing stages):
1) Diarization -> `diarization.work.json` (existing)
2) Speaker ref extraction -> `analysis/voice_refs/manifest.json` (existing)
3) Speaker profile matching:
   - Compute embedding for each speaker ref (reuse `voice_store.embeddings`)
   - Match to existing profiles under the series root
4) Persist stable profile mapping:
   - `Output/<job>/analysis/track_clone.json` (job-local)
   - `voice_store/<series_slug>/speaker_profiles.json` (series-scoped)
5) TTS selection order (updated):
   - Track profile ref (series scoped)
   - Character ref (existing `character_voice`)
   - Job-local speaker ref
   - Preset / fallback (existing)

### 3.2 Storage (minimal new records)
No new SQL tables required if we keep profiles in voice_store:
- `voice_store/<series_slug>/speaker_profiles.json`
- `voice_store/<series_slug>/speaker_profiles/<profile_id>/ref.wav`
- `voice_store/<series_slug>/episodes/<episode_key>.json` (mapping history)

Existing DB `speaker_mapping` continues to store per-job speaker->character assignments.

### 3.3 UI/UX (no new duplicate flows)
Extend existing "Voices" tab in `job_detail.html`:
- Display track profile suggestion per speaker:
  - profile id, confidence, drift status
- Actions:
  - Lock to existing profile
  - Create new profile
  - Promote profile to series character (uses existing promote flow)
- Warnings if consent is unknown (see Section 5)

### 3.4 Fallbacks and safety
- If embeddings provider unavailable, use deterministic fingerprint fallback (already in `voice_memory.embeddings`).
- If no match, create profile but mark `confidence=0` and `locked=false`.
- If privacy/minimal artifacts enabled:
  - Do not persist profiles to voice_store
  - Only use job-local refs

---

## 4) Monitoring + admin controls (feature 4)

### 4.1 Metrics endpoints (Task G)
Keep `/metrics` (Prometheus) as canonical; add admin JSON endpoints:
- `GET /api/admin/monitoring/overview`
  - job counts by state, queue depth, recent failures, avg durations
- `GET /api/admin/monitoring/queue`
  - queue backend snapshot (pending/running, per-user counts)
- `GET /api/admin/monitoring/qa`
  - QA score distribution and fail counts (from job outputs)
- `GET /api/admin/monitoring/voice`
  - clone success/fallback counts aggregated from `tts_manifest.json`

All endpoints require `require_role(Role.admin)` and use queue backend when available.

### 4.2 Admin UI
Add a real admin monitoring page:
- Route: `/ui/admin/monitoring`
- Template: `admin_monitoring.html`
- Data source: above API endpoints
- UI components:
  - Queue depth and running jobs
  - Failures in last 24h with links to jobs
  - QA score histogram (simple bar table)
  - Voice clone success rate

---

## 5) Licensing/consent metadata (friends-only safe) (feature 5)

### 5.1 Schema (Task F)
Extend existing series voice data without a new DB system:
- Add columns to `character_voice` table (jobs.db):
  - `consent_status` (granted|denied|unknown|pending)
  - `license_scope` (internal|friends_only|commercial|public)
  - `allowed_audience` (friends_only|internal|public)
  - `consent_source` (upload|promote_ref|import|legacy)
  - `consent_expires_at` (epoch seconds, nullable)
  - `consent_updated_by`, `consent_updated_at`
- Mirror into `voice_store/<series_slug>/characters/<character_slug>/meta.json` and `versions.json`.

### 5.2 Inference rules
Conservative, friends-only by default:
- If consent fields missing or unknown:
  - Treat as `consent_status=unknown`, `license_scope=friends_only`
  - Restrict to owner-only or friends-only use
- If voice ref source is a job with `privacy_mode` or `minimal_artifacts`:
  - Default to `unknown` and block sharing outside owner
- If user explicitly checks consent during upload/promote:
  - Set `consent_status=granted`
- If consent expired:
  - Treat as `denied` for sharing; allow private use only if owner matches
- Always choose the most restrictive status when conflicting.

### 5.3 UI prompts and enforcement
- In `voice_detail.html`:
  - Consent status banner
  - Admin/editor controls to update consent
  - Warning when consent is unknown or expired
- In job "Voices" tab:
  - Prompt on "Promote to series voice" to collect consent
  - Disable "Share to library" if any mapped voice has unknown consent
- API enforcement:
  - Block voice ref audio downloads if consent is not granted and caller is not owner/admin
  - For shared jobs, disallow using unknown-consent profiles unless overridden by admin

---

## 6) Minimum schema changes (Task B)

### 6.1 SQL tables/columns (jobs.db)
Additive changes only, via `JobStore` schema init:
1) `series_glossary` table:
   - `series_slug TEXT PRIMARY KEY`
   - `data_json TEXT NOT NULL`
   - `updated_at REAL`
   - `updated_by TEXT`
2) Extend `character_voice` table:
   - Add columns listed in Section 5.1

No new SQL tables are required for QA workflow or track clone if profile data stays in voice_store JSON.

### 6.2 File-based records (non-SQL)
- `Output/<job>/analysis/track_clone.json`
- `voice_store/<series_slug>/speaker_profiles.json`
- Optional: `Output/_state/glossaries/<series_slug>.json`

---

## 7) Endpoint list (new + existing)

### 7.1 QA workflow
New:
- `GET /api/jobs/{id}/qa/summary`
- `GET /api/jobs/{id}/qa/segments`
- `POST /api/jobs/{id}/qa/run`
- `POST /api/jobs/{id}/review/segments/approve`
- `POST /api/jobs/{id}/review/synthesize`

Existing (reused):
- `/api/jobs/{id}/review/segments`, `/edit`, `/regen`, `/lock`, `/unlock`
- `/api/jobs/{id}/transcript` (read/write)
- `/api/jobs/{id}/transcript/synthesize`

### 7.2 Terminology and pronunciation
New:
- `GET /api/series/{series_slug}/glossary`
- `PUT /api/series/{series_slug}/glossary`
- `POST /api/series/{series_slug}/glossary/validate`
  - returns deterministic validation errors + safe regex constraints

### 7.3 Track clone
New:
- `GET /api/series/{series_slug}/speaker-profiles`
- `POST /api/series/{series_slug}/speaker-profiles`
- `PATCH /api/series/{series_slug}/speaker-profiles/{profile_id}`
- `POST /api/series/{series_slug}/speaker-profiles/{profile_id}/enroll`
- `GET /api/jobs/{id}/track-clone` (job-local mapping summary)

### 7.4 Consent/licensing
New:
- `GET /api/series/{series_slug}/voices/{character_slug}/consent`
- `PUT /api/series/{series_slug}/voices/{character_slug}/consent`

### 7.5 Monitoring
New admin endpoints:
- `GET /api/admin/monitoring/overview`
- `GET /api/admin/monitoring/queue`
- `GET /api/admin/monitoring/qa`
- `GET /api/admin/monitoring/voice`

---

## 8) UI pages list

Existing pages to extend:
- `/ui/jobs/{job_id}` (Review tab + QA filters + approve/rerun)
- `/ui/voices/{series_slug}/{voice_id}` (consent metadata editor)
- `/ui/admin/queue` (add small links to monitoring)

New pages:
- `/ui/library/{series_slug}/glossary`
  - Full glossary editor with validation and versioning
- `/ui/admin/monitoring`
  - Real-time stats and queue depth for admins

All pages use the existing template system and Alpine-driven UI (no wireframes).

---

## 9) Fallback rules and structured logging

Global rule: every new flow logs a structured event and must have a safe fallback.

QA workflow:
- If QA files missing, return `enabled=false` and log `qa_missing`.
- If resynth fails, keep original outputs and log `resynth_failed`.

Glossary/pronunciation:
- If glossary missing or invalid, skip and log `glossary_unavailable`.
- If lexicon apply fails, keep original text and log `pronunciation_apply_failed`.

Track clone:
- If embeddings missing, fall back to job-local refs and log `track_clone_fallback`.
- If privacy/minimal mode is on, do not persist profiles and log `track_clone_disabled_privacy`.

Consent:
- If consent unknown, enforce friends-only and log `consent_unknown_restricted`.
- If consent denied, block sharing and log `consent_denied_block`.

Monitoring:
- If queue backend unavailable, fall back to scheduler state and log `monitoring_fallback_queue`.

All write actions should emit both `audit_event(...)` and `logger.info(...)`.

---

## 10) Exact file changes list (plan)

Modify:
- `src/dubbing_pipeline/web/routes/jobs_review.py` (QA endpoints + approval + resynth wrappers)
- `src/dubbing_pipeline/qa/scoring.py` (helper to surface segment issues)
- `src/dubbing_pipeline/review/state.py` (add cached QA status, approval mirror)
- `src/dubbing_pipeline/review/ops.py` (apply approval + resynth helpers)
- `src/dubbing_pipeline/jobs/queue.py` (resynth selection; track clone mapping)
- `src/dubbing_pipeline/stages/tts.py` (pronunciation lexicon apply; track clone ref selection)
- `src/dubbing_pipeline/text/style_guide.py` (pronunciation_lexicon support)
- `src/dubbing_pipeline/stages/translation.py` (deterministic glossary fallback)
- `src/dubbing_pipeline/web/routes/library.py` (glossary CRUD endpoints)
- `src/dubbing_pipeline/jobs/store.py` (series_glossary table, consent columns)
- `src/dubbing_pipeline/voice_store/store.py` (persist consent metadata)
- `src/dubbing_pipeline/api/routes_admin.py` (monitoring endpoints)
- `src/dubbing_pipeline/web/routes_ui.py` (new admin page + glossary page)
- `src/dubbing_pipeline/web/templates/job_detail.html` (QA review controls)
- `src/dubbing_pipeline/web/templates/voice_detail.html` (consent editor)
- `src/dubbing_pipeline/web/templates/admin_queue.html` (link to monitoring)

Add:
- `src/dubbing_pipeline/web/templates/admin_monitoring.html`
- `src/dubbing_pipeline/web/templates/series_glossary.html`
- `src/dubbing_pipeline/voice_store/consent.py` (consent evaluation helpers)
- `src/dubbing_pipeline/voice_store/profiles.py` (track clone profile helpers)
- `docs/FEATURES_PRO_UPGRADE_PLAN.md` (this file)

---

## 11) Test plan and smoke plan

### 11.1 Unit/integration tests
- Add tests for:
  - QA endpoints (summary, segments, run)
  - Review approval and resynth request handling
  - Glossary CRUD validation and deterministic apply
  - Consent enforcement for voice refs download
  - Track clone profile mapping persistence
- Reuse existing test helpers:
  - `tests/test_job_detail_api.py`, `tests/test_dashboard_api.py`
  - `tests/test_voice_mapping.py`, `tests/test_voice_versioning.py`
  - `tests/test_transcript_editing.py`

### 11.2 Smoke plan (manual)
1) Submit a job with QA enabled and verify QA summary appears in Job Detail.
2) Open Review tab, approve 2 segments, regen one, then click "Rerun approved".
3) Verify resynth job enqueued and outputs updated.
4) Create a series glossary, validate it, and confirm deterministic replacements in a new job.
5) Promote a speaker ref to a series voice and set consent to "friends_only"; verify sharing is blocked.
6) Open `/ui/admin/monitoring` and verify queue depth and recent failures display.

---

## 12) Notes on integration

- Auth: use `require_scope` and `require_role` (no new auth layer).
- Library grouping: all new series endpoints must use `require_library_access`.
- Queue backend: all reruns must go through `submit_job_or_503`.
- Manifest writer: QA and track clone summaries should be referenced in `manifests/job.json` when available.
