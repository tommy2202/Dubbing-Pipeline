# Library / outputs system full plan (no duplicate code)

This document is an **implementation plan** (NO CODE CHANGES in this step). It inventories the existing implementations, identifies duplicates/conflicts, and proposes a single canonical implementation for each area:

- Job store / DB layer
- Manifest writing
- Output folder layout
- Library browsing endpoints
- UI routing/pages
- Queue limits/admin controls

Absolute rules honored:

- **No parallel systems**: if jobs/DB/manifests/queue/library already exist, we extend them.
- **Do not break existing working behavior**: keep existing paths/endpoints working; add compatibility shims where needed.

---

## 1) What exists today (inventory)

### 1.1 Job DB/store layer (jobs)

- **Job model + status/progress**
  - `src/anime_v2/jobs/models.py`
    - `JobState`: `QUEUED|PAUSED|RUNNING|DONE|FAILED|CANCELED`
    - `Job`: includes `progress` (float), `message` (str), `runtime` (dict), plus output paths (`output_mkv`, `output_srt`), `work_dir`, `log_path`.

- **Job persistence**
  - `src/anime_v2/jobs/store.py`
    - `JobStore` backed by **SqliteDict** on a single SQLite file (default: `Output/_state/jobs.db` via `src/anime_v2/server.py`).
    - Tables in the same DB file:
      - `jobs` (job_id -> job dict)
      - `idempotency` (idempotency key -> {job_id, ts})
      - `presets` (preset_id -> dict)
      - `projects` (project_id -> dict)
      - `uploads` (upload_id -> dict; resumable upload metadata)
    - No explicit SQL schema/migrations; compatibility is handled by `Job.from_dict()` defaults.

- **Auth/session DB (separate, not part of JobStore)**
  - `src/anime_v2/api/models.py`
    - `AuthStore` using `sqlite3` directly.
    - Creates SQL tables (`users`, `api_keys`, `refresh_tokens`, `qr_login_codes`, `totp_recovery_codes`) and performs **best-effort schema migration** for refresh token columns.
    - Stored under `Output/_state/auth.db` by default (`src/anime_v2/server.py`).

**Key point**: there are **two SQLite DB “styles”** in v2:

- Jobs: `SqliteDict` (KV tables, no migrations)
- Auth: `sqlite3` (SQL tables + migrations)

This is acceptable (different domains), but for “library” features we must **extend JobStore** rather than introduce a third persistence system.

---

### 1.2 Resume / “migrations” / progress tracking

- **Checkpointing (resume-safe, artifact hashing)**
  - `src/anime_v2/jobs/checkpoint.py`
    - Writes `.checkpoint.json` under the job base dir (see output layout below).
    - Tracks per-stage artifacts with sha256/mtime/size; used to skip work safely.
    - This is a “manifest-like” system but optimized for resume correctness.

- **Stage manifests (metadata, hashes, resume checks)**
  - `src/anime_v2/jobs/manifests.py`
    - `write_stage_manifest(job_dir, stage, inputs, params, outputs, completed=True)` -> `Output/<job>/manifests/<stage>.json`
    - `can_resume_stage(...)` checks manifest hashes and expected outputs.

- **Runtime scheduler + queue updates**
  - `src/anime_v2/jobs/queue.py`
    - Updates `JobStore` throughout execution (`progress`, `message`, `state`, `runtime`, etc.).
  - `src/anime_v2/runtime/scheduler.py`
    - Backpressure (degrade modes / delay) and concurrency caps; also updates the persisted job `runtime.metadata.degraded`.

---

### 1.3 Output folder layout (current de-facto contract)

The pipeline is already “layout-aware” and relies on specific paths.

- **Primary layout is implemented in**
  - `src/anime_v2/jobs/queue.py` (canonical today)

Current conventions:

- **Output root**: `get_settings().output_dir` (commonly `Output/`)
  - Set in `src/anime_v2/server.py` as `app.state.output_root`

- **Canonical per-job base dir**:
  - `Output/<stem>/` by default (`stem` comes from `video_path.stem` OR `job.runtime.source_stem` if set)
  - Optional project grouping:
    - If job runtime contains `project.output_subdir`, base dir becomes: `Output/<output_subdir>/<stem>/`

- **Per-job stable pointer directory**:
  - `Output/jobs/<job_id>/`
  - Contains:
    - `target.txt` => absolute path to the canonical base dir
    - `job_id.txt`
    - `video.txt` (redacted when privacy is enabled)
  - Implemented in `src/anime_v2/jobs/queue.py`

- **Per-run work directory**:
  - `Output/<stem>/work/<job_id>/` (temp/intermediate artifacts)

- **Logs**
  - Job log file: `Output/<stem>/job.log`
  - Structured logs: under `Output/<stem>/logs/**` (used by `src/anime_v2/utils/job_logs.py`, ffmpeg logs, etc.)

- **Other notable folders/files used by the pipeline/UI**
  - `Output/<job>/stream/manifest.json` (streaming manifest for HLS/chunks) (referenced by `src/anime_v2/web/routes_jobs.py`, `src/anime_v2/reports/drift.py`, `src/anime_v2/qa/scoring.py`)
  - `Output/<job>/review/state.json`, `review/overrides.json`, `review/audio/*` (review tooling)
  - `Output/<job>/analysis/*` (reports, QA, retention report, etc.)
  - `Output/<job>/subs/*` (subtitle variants)

- **Secondary layout helper exists but is not the canonical source**
  - `src/anime_v2/utils/paths.py`
    - `output_root()`, `output_dir_for(video_path)` etc.
    - It does **not** currently encode the full “project output_subdir + stable pointer + work/<job_id>” logic that `jobs/queue.py` uses.

---

### 1.4 Manifest writing (everything that looks like a manifest)

There are multiple “manifest-like” outputs:

- **Stage manifests**: `src/anime_v2/jobs/manifests.py` → `Output/<job>/manifests/<stage>.json`
- **Checkpoint**: `src/anime_v2/jobs/checkpoint.py` → `Output/<job>/.checkpoint.json`
- **TTS manifest**:
  - `Output/<job>/tts_manifest.json` or `Output/<job>/work/<job_id>/tts_manifest.json` (multiple readers exist, e.g. `src/anime_v2/review/ops.py`, `src/anime_v2/qa/scoring.py`)
- **Streaming manifest**:
  - `Output/<job>/stream/manifest.json` (served by `/api/jobs/{id}/stream/manifest`)
- **Backup manifest** (repository backup tooling):
  - `src/anime_v2/ops/backup.py` writes `backup-<stamp>.manifest.json` and embeds `manifest.json` in the zip
- **Domain-specific manifests**:
  - Voice memory audition/tools write `manifest.json` under `Output/<job>/audition/` (`src/anime_v2/voice_memory/audition.py`, `src/anime_v2/voice_memory/tools.py`)
  - Review overrides writes `Output/<job>/manifests/overrides.json` and `analysis/overrides_applied.jsonl` (`src/anime_v2/review/overrides.py`)

---

### 1.5 Web endpoints (jobs, playback, uploads, “library browsing”)

There are two “web layers”:

- **Core FastAPI app wiring**: `src/anime_v2/server.py`
  - Mounts and includes:
    - API routers: `src/anime_v2/api/routes_auth.py`, `routes_keys.py`, `routes_runtime.py`, `routes_settings.py`, `routes_audit.py`
    - Jobs/API+UI: `src/anime_v2/web/routes_jobs.py`, `src/anime_v2/web/routes_ui.py`, `src/anime_v2/web/routes_webrtc.py`
  - Also defines legacy/simple endpoints:
    - `/` (renders `index.html` with filesystem-scanned videos)
    - `/video/{job}` (plays a hashed-id video from `_iter_videos()`)
    - `/files/{path:path}` (range-serving from Output root; this is important and used by API results)

- **Primary jobs + uploads + “library” API**: `src/anime_v2/web/routes_jobs.py`
  - Uploads: `/api/uploads/*` (resumable uploads, chunking, encryption-at-rest)
  - File picker: `GET /api/files` (lists **Input** files only; not an output library)
  - Jobs:
    - `POST /api/jobs` create
    - `GET /api/jobs` list (filtering by archived/project/mode/tag/text, pagination)
    - `GET /api/jobs/{id}` detail
    - `GET /api/jobs/{id}/files` “output discovery” (returns URLs under `/files/...`)
    - Job control/admin: cancel/pause/resume/kill/delete, tags, archive/unarchive
    - Review/transcript endpoints, logs endpoints, streaming endpoints
  - Presets/projects CRUD:
    - `/api/presets`, `/api/projects` backed by `JobStore` tables

---

### 1.6 UI routing/pages (job submit + browse)

- **UI router**: `src/anime_v2/web/routes_ui.py` (prefix `/ui`)
  - Pages:
    - `/ui/login` (template: `login.html`)
    - `/ui/dashboard` (template: `dashboard.html`)
    - `/ui/upload` (template: `upload_wizard.html`) → job submission UI
    - `/ui/jobs/{job_id}` (template: `job_detail.html`)
    - `/ui/projects` (template: `projects.html`)
    - `/ui/presets` (template: `presets.html`)
    - `/ui/settings` (template: `settings.html`)
  - Partials:
    - `/ui/partials/jobs_table` (template: `_jobs_table.html`)

Templates live under `src/anime_v2/web/templates/`.

**Note**: UI currently contains a deliberate duplicate:

- `routes_ui._job_base_dir_from_dict()` mirrors `routes_jobs._job_base_dir()` “without importing it (avoid circular imports)”.

---

### 1.7 Queueing/concurrency limits/admin controls

There are multiple layers (each is already in use; do not add another):

- **Worker concurrency**
  - `src/anime_v2/server.py` creates `JobQueue(store, concurrency=s.jobs_concurrency)`
  - `src/anime_v2/jobs/queue.py` runs `self.concurrency` async workers consuming an internal queue.

- **Global scheduling + phase caps + backpressure**
  - `src/anime_v2/runtime/scheduler.py`
    - Caps: `max_concurrency_global`, `max_concurrency_transcribe`, `max_concurrency_tts`
    - Optional per-mode caps: `max_jobs_high`, `max_jobs_medium`, `max_jobs_low`
    - Backpressure: `backpressure_q_max`:
      - degrade mode high→medium→low when queue too long
      - if already low, delay enqueue with jitter/backoff

- **Per-user quotas / limits**
  - `src/anime_v2/jobs/limits.py`
    - `max_concurrent_per_user` (default 2)
    - `daily_processing_minutes` (default 240)
    - `max_upload_mb`, media validation caps, watchdog timeouts
  - Enforced in `src/anime_v2/web/routes_jobs.py` during job submission.

- **Request rate limiting**
  - `src/anime_v2/utils/ratelimit.py` `RateLimiter` (used in auth + jobs routes).

- **Admin/operator endpoints**
  - `src/anime_v2/api/routes_runtime.py`: `/api/runtime/state`, `/api/runtime/models`, `/api/runtime/models/prewarm`
  - `src/anime_v2/web/routes_jobs.py`: `kill`, `delete`, tags/archive controls with role gating

---

## 2) Duplicates and conflicts (what must be unified)

### 2.1 Output base-dir resolution is duplicated (and slightly inconsistent)

Implementations:

- Canonical base-dir creation: `src/anime_v2/jobs/queue.py` (uses `runtime.source_stem` and `project.output_subdir`)
- Base-dir resolution for API: `src/anime_v2/web/routes_jobs.py::_job_base_dir(job)`
  - Prefers parent of `job.output_mkv`, else `Output/<video_stem>`
  - Does **not** use the `Output/jobs/<job_id>/target.txt` pointer
- Base-dir resolution for UI: `src/anime_v2/web/routes_ui.py::_job_base_dir_from_dict(job_dict)`
  - Mirrors `routes_jobs` logic (duplicate-by-design)

Risk:

- A job may be stored under `Output/<project_subdir>/<stem>/` but some resolvers may fall back to `Output/<stem>/` if `output_mkv` isn’t set yet or paths differ.

Decision:

- **Single canonical output layout** should be defined once (see Section 3).

---

### 2.2 “Library browse” has two different sources of truth

- **Filesystem scan**:
  - `src/anime_v2/server.py::_iter_videos()` scans `Output/**` for `*.mp4/*.mkv`, generates hashed IDs, renders `/` index and serves `/video/{job}`.
- **JobStore-backed**:
  - `src/anime_v2/web/routes_jobs.py` uses `JobStore.list()` for dashboard + APIs, and `job_files()` heuristics to discover outputs.

Risk:

- Filesystem scan can show items that are not real jobs (manual copies, old artifacts) and cannot link back to job metadata/auth correctly.

Decision:

- Canonical library view should be **JobStore-first** (job metadata), with optional filesystem scan only as a fallback/tooling view.

---

### 2.3 Filtering logic is duplicated between UI and API

- API: `GET /api/jobs` in `src/anime_v2/web/routes_jobs.py` implements filtering by archived/project/mode/tag/text and pagination.
- UI: `/ui/partials/jobs_table` in `src/anime_v2/web/routes_ui.py` implements similar filtering locally (duplicated logic).

Decision:

- Canonical filter semantics should live in **one place** (prefer API implementation). UI should call it or reuse shared helpers.

---

### 2.4 “Manifest writing” is scattered across features without a single “job manifest”

We have:

- checkpoint (`.checkpoint.json`) for correctness/resume
- stage manifests (`manifests/<stage>.json`) for metadata/resume checks
- multiple special-case manifests (`tts_manifest.json`, `stream/manifest.json`, `analysis/*.json`, voice memory manifests)

Decision:

- Keep checkpoint and stage manifests as-is, but add/standardize a **single top-level job manifest** that indexes the others, so the library/UI/API don’t need ad-hoc discovery rules.

---

## 3) Canonical implementations (single source of truth)

### 3.1 Job store / DB layer (canonical)

Canonical choice:

- **Jobs**: `src/anime_v2/jobs/store.py` (`JobStore` + `jobs.db` in `Output/_state/`)
- **Auth**: `src/anime_v2/api/models.py` (`AuthStore` + `auth.db` in `Output/_state/`)

Rule:

- Do **not** introduce a new ORM layer (sqlmodel/sqlalchemy) or a second jobs DB. Extend `JobStore` tables inside the existing `jobs.db`.

Planned extensions (schema changes within `jobs.db`, via new SqliteDict tables):

- Add a table for library/output indexing (exact naming can vary, but must be within `jobs.db`):
  - `job_outputs` (job_id → dict with discovered output files, timestamps, and base_dir pointer)
  - Optional: `job_events` or `job_audit` (append-only list; only if we need it and it doesn’t duplicate existing audit logs)

Compatibility strategy:

- Keep current `jobs` records intact; all new fields must be **optional** and defaultable.
- Prefer writing derived library indexes **best-effort** (never block the job pipeline).

---

### 3.2 Output folder layout (canonical)

Canonical choice:

- The layout encoded in `src/anime_v2/jobs/queue.py` is the contract:
  - base dir: `Output/<project_subdir?>/<stem>/`
  - pointer dir: `Output/jobs/<job_id>/target.txt`
  - work dir: `Output/<...>/work/<job_id>/`

Plan:

- Make `src/anime_v2/utils/paths.py` the single canonical module for all layout derivations by **extending it** (not creating a parallel “layout” module).

Files to modify later:

- `src/anime_v2/utils/paths.py` (add canonical helpers)
- `src/anime_v2/jobs/queue.py` (use the helper; preserve exact behavior)
- `src/anime_v2/web/routes_jobs.py` (replace `_job_base_dir()` with helper that prefers `Output/jobs/<job_id>/target.txt`)
- `src/anime_v2/web/routes_ui.py` (delete mirror logic; import helper safely)
- `src/anime_v2/server.py` (stop filesystem scanning for library; prefer JobStore or redirect)

Proposed helper API (no new behavior; just centralization):

- `job_base_dir_for(job: Job) -> Path`
  - Prefer `Output/jobs/<job_id>/target.txt` when present
  - Else fall back to `job.output_mkv` parent
  - Else fall back to `Output/<video_stem>`
- `job_pointer_dir(job_id: str) -> Path` → `Output/jobs/<job_id>/`
- `job_work_dir(base_dir: Path, job_id: str) -> Path` → `base_dir/work/<job_id>/`

---

### 3.3 Manifest writing (canonical)

Canonical choice:

- Stage manifest writing: `src/anime_v2/jobs/manifests.py`
- Resume checkpoints: `src/anime_v2/jobs/checkpoint.py` (keep; do not merge with manifests)

Missing piece (to avoid ad-hoc discovery):

- Add a canonical “job manifest index”:
  - `Output/<job>/manifests/job.json`
  - This should be a **thin index** referencing:
    - job metadata (id, owner_id, created_at, mode, device, privacy flags)
    - canonical base_dir, work_dir (or last work_dir)
    - known outputs (mkv/mp4/hls/subs)
    - pointers to stage manifests (`manifests/audio.json`, `manifests/transcribe.json`, etc.)
    - pointers to special manifests if they remain (e.g. `stream/manifest.json`, `tts_manifest.json`)

Schema changes required:

- New JSON schema for `manifests/job.json` (versioned):
  - `version: 1`
  - `job: {id, owner_id, created_at, updated_at, mode, device, src_lang, tgt_lang, state}`
  - `layout: {output_root, base_dir_rel, jobs_ptr_rel, work_dir_rel?}`
  - `manifests: {stage: "manifests/<stage>.json", ...}`
  - `special: {stream_manifest?, tts_manifest?, review_state?, qa_summary?}`
  - `outputs: {primary: {...}, mobile: {...}, tracks: [...], subs: [...] }`

Files to modify later:

- `src/anime_v2/jobs/manifests.py` (add helper to write/read `manifests/job.json` using existing `write_json`)
- `src/anime_v2/jobs/queue.py` (write/update the job index manifest at submit/start/end; best-effort)
- `src/anime_v2/web/routes_jobs.py` (prefer reading `manifests/job.json` for `job_files()` and job detail; fall back to heuristics)

---

### 3.4 Library browsing endpoints (canonical)

Canonical choice:

- **API** is the canonical “library”:
  - `GET /api/jobs` (list)
  - `GET /api/jobs/{id}` (detail)
  - `GET /api/jobs/{id}/files` (outputs)
  - `GET /files/{path:path}` (range-serving for actual bytes; already canonical)
  - `GET /api/projects` and `GET /api/presets` (metadata for browsing/submission)

Existing duplicates to deprecate:

- `GET /` + `GET /video/{job}` in `src/anime_v2/server.py` are a second library view.

Endpoint definitions (existing; will be extended but not replaced):

- `GET /api/jobs`
  - Request params: `status/state`, `q`, `project`, `mode`, `tag`, `include_archived`, `limit`, `offset`
  - Response: `{items, limit, offset, total, next_offset}`
  - Plan: add optional, backwards-compatible fields:
    - `project_name` (already derivable from job.runtime)
    - `archived` (already in runtime)
    - `outputs_summary` (kinds present: mkv/mp4/hls/mobile/etc.)

- `GET /api/jobs/{id}/files`
  - Currently performs filesystem heuristics (glob/rglob) to find outputs and returns URLs under `/files/...`.
  - Plan: prefer `manifests/job.json` (and/or a `JobStore` `job_outputs` cache) to avoid duplicating heuristics across UI/API and to make results deterministic.

Files to modify later:

- `src/anime_v2/web/routes_jobs.py` (centralize output discovery; return a stable schema)
- `src/anime_v2/server.py`
  - Option A (preferred): make `/` redirect to `/ui/dashboard`
  - Option B: keep `/` but source it from `JobStore.list()` + `job_files()` (no filesystem scan hashing)
  - Keep `/files/*` as-is (it is the canonical file server)

---

### 3.5 UI routing/pages (canonical)

Canonical choice:

- `/ui/*` pages in `src/anime_v2/web/routes_ui.py` + templates under `src/anime_v2/web/templates/`.

Plan:

- Treat `dashboard.html` as the canonical “library browse” view.
- Treat `upload_wizard.html` as the canonical job submission UI.
- Make the root page `/` a thin redirect or wrapper (do not maintain a second browse UX).

UI page flow (canonical):

- **Unauthed**
  - `GET /login` → redirect to `/ui/login` (already)
  - `/ui/login` → login form + token cookie flow
- **Authed**
  - `/ui/dashboard` → calls `/ui/partials/jobs_table` (or directly `/api/jobs`) to list jobs
  - `/ui/upload` → wizard:
    - file picker via `GET /api/files` (Input browsing) and/or upload endpoints (`/api/uploads/*`)
    - job submit via `POST /api/jobs`
  - `/ui/jobs/{job_id}` → job detail:
    - job info via `GET /api/jobs/{id}`
    - outputs via `GET /api/jobs/{id}/files`
    - log streaming via `/api/jobs/{id}/logs/stream`

Duplicates to remove later (reroute):

- `src/anime_v2/web/routes_ui.py::_job_base_dir_from_dict()` should be removed and replaced with the shared `utils/paths` helper.
- `/ui/partials/jobs_table` should not duplicate filter logic; it should either:
  - call `GET /api/jobs` internally, or
  - import a shared filter helper used by both UI and API.

---

### 3.6 Queue policy defaults + admin controls (canonical)

Canonical choices:

- Global/phase/mode scheduling: `src/anime_v2/runtime/scheduler.py`
- Worker execution: `src/anime_v2/jobs/queue.py`
- Per-user and upload/media limits: `src/anime_v2/jobs/limits.py`
- Admin visibility: `src/anime_v2/api/routes_runtime.py` + UI settings page

Queue policy defaults (as implemented today; to be documented and kept stable):

- **Per-user**
  - `max_concurrent_per_user = 2` (default in `jobs/limits.py`)
  - `daily_processing_minutes = 240` (default)
- **Global**
  - `MAX_CONCURRENCY_GLOBAL` (default from settings; scheduler enforces)
  - Worker pool (`jobs_concurrency`) is a second cap: it should be ≥ global cap; scheduler is the “policy” cap.
- **By mode**
  - Optional caps: `MAX_JOBS_HIGH|MAX_JOBS_MEDIUM|MAX_JOBS_LOW` (0 means fall back to global)
- **Backpressure**
  - `BACKPRESSURE_Q_MAX` (default 6):
    - degrade high→medium→low when queue too long
    - delay low jobs when queue too long

Admin controls (existing):

- `GET /api/runtime/state` (operator)
- `POST /api/jobs/{id}/kill` (admin)
- `DELETE /api/jobs/{id}` (admin)
- `POST /api/jobs/{id}/pause|resume` (submit scope; effectively owner/admin)

Plan (avoid parallel “admin control plane”):

- Keep env/config as source-of-truth for caps (no new DB-based config system unless absolutely necessary).
- If dynamic tuning is needed later, extend the existing settings store (`src/anime_v2/api/routes_settings.py` + `UserSettingsStore`) rather than adding a new config DB.

---

## 4) Concrete implementation plan (what to change, where)

This is sequenced to minimize risk and avoid behavior breaks.

### Phase 0 — Baseline: codify the canonical layout + discovery (no behavior change)

- **Add canonical layout helpers** to `src/anime_v2/utils/paths.py` (extend existing module).
- Update call sites to use the helpers while preserving current behavior:
  - `src/anime_v2/jobs/queue.py` (creation)
  - `src/anime_v2/web/routes_jobs.py` (resolution)
  - `src/anime_v2/web/routes_ui.py` (resolution; delete mirror)

Exactly what duplicate code gets removed/rerouted:

- Remove `routes_ui._job_base_dir_from_dict()` and use `utils/paths.job_base_dir_for(...)`.
- Replace `routes_jobs._job_base_dir()` with the same helper (prefer `Output/jobs/<job_id>/target.txt`).

---

### Phase 1 — Canonical “job manifest index” (reduce output discovery duplication)

- Add `Output/<job>/manifests/job.json` writing (best-effort):
  - Prefer implementing in `src/anime_v2/jobs/manifests.py` alongside stage manifests (no new “manifest system”).
- Update `src/anime_v2/jobs/queue.py` to write/update it:
  - on job creation (initial metadata + planned paths)
  - on job start (work_dir, state RUNNING)
  - on stage completion (link stage manifests)
  - on job finish (final outputs)

Schema changes required:

- New schema for `manifests/job.json` (versioned, additive only).
- Optionally add a `version` field to stage manifests (additive).

Exactly what duplicate code gets removed/rerouted:

- `src/anime_v2/web/routes_jobs.py::job_files()` should prefer `manifests/job.json` and fall back to heuristics only if missing.

---

### Phase 2 — Centralize “output discovery” into one reusable function

Create a shared, import-safe function (do not create a parallel system; this is a refactor):

- New module proposal: `src/anime_v2/jobs/outputs.py` (or similar under existing `jobs/` namespace)
  - `discover_outputs(job: Job, base_dir: Path) -> dict[str, Any]`
  - Implementation order:
    - If `manifests/job.json` exists → read it and normalize output URLs/paths
    - Else run the existing heuristic logic from `job_files()` (single copy)

Files to modify later:

- `src/anime_v2/web/routes_jobs.py` (use `discover_outputs`)
- `src/anime_v2/web/routes_ui.py` and templates (consume the API results; do not reimplement)

Exactly what duplicate code gets removed/rerouted:

- Remove heuristic duplication risk by having **one** discovery function used by all endpoints/templates.

---

### Phase 3 — Library browsing UX: remove the “filesystem scan library” from `/`

Files to modify later:

- `src/anime_v2/server.py`

Plan:

- Keep `/files/{path:path}` as-is (canonical file server).
- Change `/` (and optionally `/video/{job}`) to avoid filesystem scan:
  - Preferred: redirect `/` → `/ui/dashboard` (minimal behavior change; still shows a library)
  - If `/` must remain as a simple browse page, render it from JobStore list + `job_files()` (no hashed IDs).

Exactly what duplicate code gets removed/rerouted:

- Remove `_iter_videos()` + `_resolve_job()` usage for user-facing browsing (or keep only behind an admin-only debug route).

---

### Phase 4 — Filtering logic: make API the single canonical implementation

Files to modify later:

- `src/anime_v2/web/routes_ui.py` (`ui_jobs_table`)
- `src/anime_v2/web/routes_jobs.py` (`list_jobs`)

Plan:

- UI partial should not reimplement filtering. Options (choose one):
  - **Option A (preferred)**: UI partial calls `GET /api/jobs` and passes through results to template.
  - Option B: extract filter helper into a shared function (e.g. `anime_v2/jobs/query.py`) used by both UI and API.

Exactly what duplicate code gets removed/rerouted:

- Remove the duplicated project/mode/tag/text/archived filtering loop in `ui_jobs_table`.

---

## 5) Summary: exactly what will be removed/rerouted (duplicate-code elimination list)

When implementing, the following duplicates should be removed or rerouted to a single canonical implementation:

- **Output base-dir resolver duplication**
  - Remove `src/anime_v2/web/routes_ui.py::_job_base_dir_from_dict`
  - Replace `src/anime_v2/web/routes_jobs.py::_job_base_dir`
  - Canonicalize via `src/anime_v2/utils/paths.py` helpers that prefer `Output/jobs/<job_id>/target.txt`

- **Library listing duplication**
  - Deprecate `/` filesystem scan (`src/anime_v2/server.py::_iter_videos`)
  - Make `/` redirect to `/ui/dashboard` or render from JobStore

- **Filtering duplication**
  - Remove UI-side filtering in `/ui/partials/jobs_table`; use `GET /api/jobs` filtering semantics

- **Output discovery duplication risk**
  - Move the heuristic output discovery currently inside `GET /api/jobs/{id}/files` into a shared function
  - Add `Output/<job>/manifests/job.json` as the canonical, deterministic source to avoid repeated heuristics

---

## 6) Notes on what we will *not* do (to avoid parallel systems)

- No new DB/ORM for jobs (extend `JobStore` tables only).
- No new “manifest subsystem” outside `anime_v2/jobs/manifests.py` (add job index manifest there).
- No new “library service” that separately scans Output and invents IDs (JobStore remains canonical).
- No new queueing layer (Scheduler + JobQueue + Limits remain canonical).

