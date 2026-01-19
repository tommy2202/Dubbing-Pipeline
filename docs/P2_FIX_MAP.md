## P2 Fix Map (plan only)

Scope: P2 items A1-A2, B3-B4, C5-C6 only. No P0/P1 changes beyond safe integration.

### Canonical modules to reuse (do not duplicate)

**Library browsing + templates**
- API: `src/dubbing_pipeline/api/routes_library.py` (library endpoints). 
- Queries: `src/dubbing_pipeline/library/queries.py` (search, recent ordering, visibility filters). 
- UI routes: `src/dubbing_pipeline/web/routes_ui.py` (library pages). 
- Templates: `src/dubbing_pipeline/web/templates/library_series.html`, `library_seasons.html`, `library_episodes.html`, `library_episode_detail.html`. 
- Library path/manifest: `src/dubbing_pipeline/library/paths.py`, `src/dubbing_pipeline/library/manifest.py`. 
- Library schema: `src/dubbing_pipeline/jobs/store.py` (`job_library` table). 

**Job outputs + file serving**
- Outputs index: `src/dubbing_pipeline/web/routes_jobs.py` (`/api/jobs/{id}/files`, `/api/jobs/{id}/outputs`). 
- Output roots + library roots: `src/dubbing_pipeline/library/paths.py`. 
- Output file serving guard: `src/dubbing_pipeline/server.py` (`/files/*` serving, output root checks). 

**Transcode/export steps**
- Export helpers: `src/dubbing_pipeline/stages/export.py` (MP4/HLS/mobile export, M4A). 
- MKV mux/export: `src/dubbing_pipeline/stages/mkv_export.py`. 
- Pipeline orchestration: `src/dubbing_pipeline/jobs/queue.py` (export + mobile artifacts). 

**Voice refs store + persistence**
- Voice refs extraction: `src/dubbing_pipeline/voice_refs/extract_refs.py`. 
- Series voice store: `src/dubbing_pipeline/voice_store/store.py` (history in `refs/`, meta/index writes). 
- Voice embeddings: `src/dubbing_pipeline/voice_store/embeddings.py` (optional, graceful). 
- DB schema: `src/dubbing_pipeline/jobs/store.py` (`character_voice`, `speaker_mapping`). 
- Routes + audio access: `src/dubbing_pipeline/web/routes_jobs.py` (voice refs and character endpoints). 
- UI: `src/dubbing_pipeline/web/templates/job_detail.html` (Voices tab). 

**Diarization outputs + speaker samples**
- Diarization stage: `src/dubbing_pipeline/stages/diarization.py`. 
- Speaker ref extraction hook: `src/dubbing_pipeline/jobs/queue.py` (`voice_refs` extraction, speaker mapping). 

### P2 implementation plan (file-by-file)

#### A1) Preview variants (low-res MP4 + audio-only preview)
**Goal:** Add lightweight preview artifacts in Output + Library with no GPU dependency.

Planned changes:
- `src/dubbing_pipeline/stages/export.py`
  - Add `export_preview_mp4(...)` (low-res, low bitrate, short GOP).
  - Add `export_preview_audio(...)` (AAC or M4A using existing `export_m4a`).
  - Guard with ffmpeg availability; log and skip on failure.
- `src/dubbing_pipeline/jobs/queue.py`
  - After main export, generate preview variants under `Output/<job>/preview/`.
  - Populate output manifest with preview paths (best-effort).
- `src/dubbing_pipeline/library/paths.py`
  - Extend `mirror_outputs_best_effort(...)` to link preview files into Library view.
- `src/dubbing_pipeline/library/manifest.py`
  - Include preview paths (mp4_preview, audio_preview) in library manifest.
- `src/dubbing_pipeline/web/routes_jobs.py`
  - Extend `/api/jobs/{id}/files` to emit preview file entries and player keys.
- `src/dubbing_pipeline/web/templates/job_detail.html`
  - Add preview playback links in job detail outputs list.
- `src/dubbing_pipeline/web/templates/library_episode_detail.html`
  - Add preview playback links in library episode view.
- New verifier: `scripts/verify_preview_variants.py`
  - Generate tiny ffmpeg media, run export helpers, assert preview files exist.
  - Skip when ffmpeg missing.

Avoid duplication:
- Use existing `export_mp4`/`export_m4a` helpers and library manifest.
- Do not add new file serving endpoints; reuse `/files/*` and `/api/jobs/{id}/files`.

#### A2) Library browsing polish (search/filter, recent, continue last series)
**Goal:** Improve library navigation without changing auth or schema.

Planned changes:
- `src/dubbing_pipeline/library/queries.py`
  - Reuse existing `q`, `order=recent`, and `view` filters.
  - Add a small “continue last series” query (latest updated series per user).
- `src/dubbing_pipeline/api/routes_library.py`
  - Add `GET /api/library/continue` (returns most recent series for current user).
  - Add explicit `order=recent` wiring (already supported).
- `src/dubbing_pipeline/web/templates/library_series.html`
  - Add search bar, view toggles, recent ordering dropdown.
  - Add “Continue last series” card using `/api/library/continue`.
- `src/dubbing_pipeline/web/templates/library_seasons.html` + `library_episodes.html`
  - Add search + visibility filters (consistent with API).

Avoid duplication:
- Keep all list logic inside `library/queries.py`.
- Keep RBAC via `require_scope("read:job")` and visibility filters.

#### B3) Voice mapping review UI (listen + confirm before persist)
**Goal:** Add a review step before saving speaker->character mapping into series store.

Planned changes:
- `src/dubbing_pipeline/web/templates/job_detail.html`
  - Add a “Review & confirm” step for speaker mapping.
  - Preview speaker samples (use existing `/api/jobs/{id}/voice_refs/.../audio`).
- `src/dubbing_pipeline/web/routes_jobs.py`
  - Add endpoint to stage mappings in job runtime (job-scoped, not series).
  - Add endpoint to confirm and persist to series voice store.
  - Reuse existing `save_character_ref` for final persistence.
- `src/dubbing_pipeline/voice_store/store.py`
  - No schema changes; reuse history `refs/` as version trail.
- New verifier: `scripts/verify_voice_mapping_review.py`
  - Create job with fake voice refs manifest, test review/confirm endpoints.

Avoid duplication:
- Reuse `get_job_voice_refs`, `speaker_mapping` table, and series `character_voice`.
- Avoid new storage; stage mappings in job runtime or existing DB tables.

#### B4) Voice drift detection + rollback/history for character voices
**Goal:** Detect drift when new refs are saved and allow rollback to prior refs.

Planned changes:
- `src/dubbing_pipeline/voice_store/store.py`
  - Add helper to list historical refs from `refs/`.
  - Add helper to rollback (copy a historical ref to `ref.wav` + update meta/index).
- `src/dubbing_pipeline/voice_store/embeddings.py`
  - Use optional embeddings to compute similarity for drift detection.
  - If embeddings unavailable, mark drift as “unknown” (graceful).
- `src/dubbing_pipeline/web/routes_jobs.py`
  - Add endpoints:
    - `GET /api/series/{slug}/characters/{cslug}/versions`
    - `POST /api/series/{slug}/characters/{cslug}/rollback`
  - Include drift score + provider in response where available.
- `src/dubbing_pipeline/web/templates/job_detail.html` (or new series settings page)
  - Display version history and drift warnings; allow rollback with confirm.
- New verifier: `scripts/verify_voice_drift.py`
  - Simulate two refs, check history list + rollback path.

Avoid duplication:
- Use existing voice store history layout (`refs/`).
- Use existing embedding helper; do not add new embedding pipeline.

#### C5) Postgres metadata option OR single-writer strategy (optional)
**Goal:** Provide optional scale path without forcing changes.

Planned changes (documented and gated):
- Add a `JobStore` adapter interface (SQLite default).
- Optional Postgres adapter for library/voice tables only (job blobs remain local).
- Alternative: “single-writer mode” documented (one writer, read replicas).
- Add a doc section explaining tradeoffs + setup.

Avoid duplication:
- Keep default SQLite behavior unchanged.
- Gate Postgres via config flag and environment variables.

#### C6) Optional migrations framework (Alembic, only if schema evolves)
**Goal:** Provide optional migration tooling without forcing use.

Planned changes:
- Add Alembic config and baseline migration for `job_library` + voice tables.
- Only run when `USE_ALEMBIC=1` or when Postgres adapter is enabled.
- Keep `JobStore._init_*` as fallback for SQLite.

### Verifiers (lightweight, CPU-only)
- `scripts/verify_preview_variants.py`
- `scripts/verify_voice_mapping_review.py`
- `scripts/verify_voice_drift.py`
- `scripts/verify_library_continue.py` (optional, TestClient-based)

All verifiers should:
- Use tiny ffmpeg-generated media or fixtures.
- Skip gracefully if optional deps (ffmpeg/fastapi/embeddings) are missing.

### Logging + security guardrails
- Preserve existing RBAC/ownership checks (`require_scope`, `_require_job_access`, `require_library_owner_or_admin`).
- Log preview and voice changes with `job_id`, `series_slug`, `character_slug`, `request_id`.
- Do not serve files outside Output root; reuse `/files/*` guardrails.
