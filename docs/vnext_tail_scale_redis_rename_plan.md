# vNext plan: Tailscale golden-path + Redis queue (L2) + full rename to “Dubbing Pipeline”

This document is an **implementation plan** (not code) for three repo-wide changes:

1. **Tailscale golden-path docs + run scripts** (single canonical “phone on cellular → web UI” path).
2. **Redis queue + SQLite metadata (Level 2)** with **automatic fallback to Level 1** (current in-proc queue) when Redis is down.
3. **Full rename to “Dubbing Pipeline”** with **no “anime” or “v1/v2/alpha” references** in the final repo state, while **not breaking existing functionality** and providing a **migration path**.

Absolute rules honored by this plan:
- **No duplicate systems**: extend/reuse existing modules for jobs/queue/db/logging/auth.
- **Do not break existing functionality**: ship a migration path with compatibility layers and staged cutovers.
- Identify all “anime” and “v1/v2” appearances across code/docs/CLI/UI/env/docker/compose/package/import paths.
- Identify all DB usage (sqlite/sqlitedict/etc.) and all queue usage (if any).

---

## 0) Repo scan summary (what exists today)

### 0.1 Canonical runtime architecture (current)
- **Web server**: `src/anime_v2/server.py` (FastAPI app).
- **Queue / execution (Level 1)**: `src/anime_v2/jobs/queue.py`
  - In-process `asyncio.Queue` + worker tasks.
  - “Durable-ish restart recovery”: on boot, scans `JobStore` for `QUEUED/RUNNING` and requeues.
- **Scheduling/backpressure**: `src/anime_v2/runtime/scheduler.py`
  - In-process priority heap + phase concurrency semaphores.
  - **Optional Redis usage already exists**: `_RedisMutex` used as best-effort multi-instance dispatch mutex.
- **Job metadata DB**: `src/anime_v2/jobs/store.py`
  - Uses `SqliteDict` tables (`jobs`, `idempotency`, `presets`, `projects`, `uploads`) in a single SQLite file.
  - Has an **additional SQL table** `job_library` (for indexed library browsing).
- **Auth DB**: `src/anime_v2/api/models.py` (`AuthStore`)
  - Pure `sqlite3` tables: `users`, `api_keys`, `refresh_tokens`, `qr_login_codes`, `totp_recovery_codes`.
- **Rate limiting**: `src/anime_v2/utils/ratelimit.py`
  - Redis optional backend; falls back to in-proc dict.
- **Remote access hardening**: `src/anime_v2/api/remote_access.py`
  - `REMOTE_ACCESS_MODE=tailscale|cloudflare|off` with allowlists and optional Cloudflare Access JWT verification.

### 0.2 Existing Tailscale docs/scripts
- Docs:
  - `docs/remote_access.md` (Tailscale primary, Cloudflare optional)
  - `docs/mobile_remote.md`
- Scripts:
  - `scripts/remote/tailscale_setup.md`
  - `scripts/remote/tailscale_check.py` (prints Tailscale IP + URL)

### 0.3 Existing “anime” / “v1/v2” / “alpha” references (hotspots)
The string families appear broadly in:
- **Python package/import paths**: `src/anime_v1/**`, `src/anime_v2/**`, plus many `from anime_v2...` imports.
- **Packaging/entrypoints**: `pyproject.toml`
  - `project.name = "anime-dubbing-v1"`
  - scripts: `anime-v1`, `anime-v2`, `anime-v2-web`, plus aliases `dub`, `dub-web`.
  - package include: `include = ["anime_v1*", "anime_v2*", "config*"]`
- **Runtime env vars & defaults**: `config/public_config.py`
  - `ANIME_V2_OUTPUT_DIR`, `ANIME_V2_LOG_DIR`, `ANIME_V2_STATE_DIR`, `ANIME_V2_*` policy toggles.
  - “legacy v1 defaults”: `V1_OUTPUT_DIR`, `V1_HOST`, `V1_PORT`.
  - default OTEL service name: `otel_service_name="anime_v2"`.
- **Docker/deploy**:
  - `docker/docker-compose.yml`: `ANIME_V2_OUTPUT_DIR`, Tailscale sidecar commented `hostname: anime-v2`, image labels in docs.
  - `deploy/compose.public.yml`: GHCR example uses `ghcr.io/.../anime-v2`, sets `ANIME_V2_OUTPUT_DIR`, `ANIME_V2_LOG_DIR`.
- **Docs and UI templates**: pervasive references to `anime-v2`, `anime-v1`, and “anime” wording.
- **Redis key strings / cache filenames / user-agent**:
  - `src/anime_v2/runtime/scheduler.py`: mutex key `anime_v2:scheduler:dispatch`, thread name `anime_v2.scheduler`.
  - `src/anime_v2/api/remote_access.py`: `/tmp/anime_v2_cf_access_jwks.json`, UA `anime-v2/remote-access`.

This plan’s rename work must reach **all of the above**, plus tests and CI docs.

---

## 1) Canonical modules to change (file paths)

This section enumerates the **single canonical** modules to extend (no parallel systems) and the renames that must be applied.

### 1.1 Tailscale golden path (docs + scripts)
- `docs/remote_access.md` (will be superseded/linked; keep as deep-dive)
- `docs/mobile_remote.md` (will be superseded/linked; keep as short mobile view)
- `scripts/remote/tailscale_check.py` (rename + extend into golden-path helper)
- `scripts/remote/tailscale_setup.md` (convert into “golden path” doc or keep as historical)
- **New**: `docs/GOLDEN_PATH_TAILSCALE.md` (outline in §4)
- **New**: run scripts under `scripts/remote/` (outline in §5)

### 1.2 Queue + DB (Level 2 Redis queue with SQLite metadata, fallback to Level 1)
Reuse these existing modules as the canonical “jobs/queue/db” system:
- `src/anime_v2/jobs/queue.py` (current Level 1 executor)
- `src/anime_v2/runtime/scheduler.py` (existing scheduling/backpressure/caps; already has Redis optional hooks)
- `src/anime_v2/jobs/store.py` (single jobs DB file; extend with queue metadata tables)
- `src/anime_v2/server.py` (lifespan wiring: store, queue, scheduler installation)
- `src/anime_v2/utils/ratelimit.py` (already provides Redis fallback pattern; mirror this for queue)
- `config/public_config.py` / `config/secret_config.py` (Redis URL already exists; add queue toggles safely)

### 1.3 Rename “anime/v1/v2/alpha” → “Dubbing Pipeline”
Packaging + imports + runtime config + docs + UI:
- `pyproject.toml`
- `main.py` (imports)
- `src/anime_v2/**` and `src/anime_v1/**` (package directory names and all internal imports)
- `config/public_config.py`, `config/secret_config.py`, `config/settings.py`
- `docker/docker-compose.yml`, `deploy/compose.public.yml`, `deploy/compose.tunnel.yml`, `docker/Dockerfile*`, `docker/Makefile`
- `.env.example`, `.env.secrets.example`
- `README.md`, `README-deploy.md`, all `docs/**/*.md`
- `scripts/**/*.py`, `scripts/**/*.md`
- `src/anime_v2/web/templates/**/*.html` (user-facing labels and links)
- `.github/workflows/ci.yml` (if it references package names/scripts/images)

---

## 2) Rename mapping (old → new)

This is the **target end-state** mapping. The migration strategy to get there without breakage is in §6.

### 2.1 Project + package naming
- **Repository name/branding**
  - `Dubbing-Pipeline` / “anime dubbing” wording → **“Dubbing Pipeline”**
- **Python project name**
  - `anime-dubbing-v1` → `dubbing-pipeline`
  - Description: remove “anime”, “v1”, “slice”, “alpha/beta” wording.
- **Primary Python package**
  - `anime_v2` → `dubbing_pipeline`
  - `anime_v1` → `dubbing_pipeline_legacy` (or `dubbing_pipeline_compat`; avoid `v1`)
  - `src/anime_v2/web/...` remains conceptually “web”, but module path becomes `dubbing_pipeline.web...`

### 2.2 CLI entrypoints (console scripts)
Current scripts in `pyproject.toml` include:
- `anime-v1`
- `anime-v2`
- `anime-v2-web`
- `dub`
- `dub-web`

Target end-state:
- `dubbing-pipeline` (main CLI; replaces `anime-v2`)
- `dubbing-pipeline-web` (server; replaces `anime-v2-web`)
- Keep short aliases:
  - `dub` → points to `dubbing_pipeline.cli:cli` (keep)
  - `dub-web` → points to `dubbing_pipeline.web.run:main` (keep)
- Legacy CLI (optional) renamed:
  - `anime-v1` → `dubbing-pipeline-legacy` (or `dub-legacy`)

### 2.3 Docker images / compose service names
- GHCR image example:
  - `ghcr.io/<owner>/anime-v2:latest` → `ghcr.io/<owner>/dubbing-pipeline:latest`
- Compose comments / hostname:
  - `hostname: anime-v2` → `hostname: dubbing-pipeline`
- Service names may remain `api/caddy/cloudflared` (already generic), but all image tags, labels, and docs must drop “anime”.

### 2.4 Env vars (public config)
Current public-config aliases contain many `ANIME_V2_*` and some `V1_*`. Target:
- `ANIME_V2_OUTPUT_DIR` → `DUBBING_PIPELINE_OUTPUT_DIR`
- `ANIME_V2_LOG_DIR` → `DUBBING_PIPELINE_LOG_DIR`
- `ANIME_V2_STATE_DIR` → `DUBBING_PIPELINE_STATE_DIR`
- `ANIME_V2_AUTH_DB_NAME` → `DUBBING_PIPELINE_AUTH_DB_NAME`
- `ANIME_V2_JOBS_DB_NAME` → `DUBBING_PIPELINE_JOBS_DB_NAME`
- `ANIME_V2_CACHE_DIR` → `DUBBING_PIPELINE_CACHE_DIR`
- `ANIME_V2_SETTINGS_PATH` → `DUBBING_PIPELINE_SETTINGS_PATH`
- `ANIME_V2_UI_AUDIT_PAGE_VIEWS` → `DUBBING_PIPELINE_UI_AUDIT_PAGE_VIEWS`
- Submission policy:
  - `ANIME_V2_MAX_ACTIVE_JOBS_PER_USER` → `DUBBING_PIPELINE_MAX_ACTIVE_JOBS_PER_USER`
  - `ANIME_V2_MAX_QUEUED_JOBS_PER_USER` → `DUBBING_PIPELINE_MAX_QUEUED_JOBS_PER_USER`
  - `ANIME_V2_DAILY_JOB_CAP` → `DUBBING_PIPELINE_DAILY_JOB_CAP`
  - `ANIME_V2_HIGH_MODE_ADMIN_ONLY` → `DUBBING_PIPELINE_HIGH_MODE_ADMIN_ONLY`
- “V1” legacy defaults:
  - `V1_OUTPUT_DIR` / `V1_HOST` / `V1_PORT` → `LEGACY_OUTPUT_DIR` / `LEGACY_HOST` / `LEGACY_PORT` (or delete if legacy CLI is moved out)
- OTEL:
  - `otel_service_name="anime_v2"` → `otel_service_name="dubbing_pipeline"`

Migration requirement (“do not break”): for one transition window, the loader should accept both names (details in §6), then drop old names in the final cut.

### 2.5 Redis key naming (and other string artifacts)
Target prefixes:
- Current (examples):
  - `anime_v2:scheduler:dispatch`
  - rate limit keys are arbitrary strings (not namespaced today)
  - `/tmp/anime_v2_cf_access_jwks.json`
  - UA `anime-v2/remote-access`
- Target:
  - Namespace everything queue/scheduler under: `dubbing_pipeline:*` (or shorter `dp:*`)
  - Cache paths: `/tmp/dubbing_pipeline_cf_access_jwks.json`
  - UA: `dubbing-pipeline/remote-access`

---

## 3) DB + queue inventory (current usage)

### 3.1 SQLite / SqliteDict usage (current)
- `src/anime_v2/jobs/store.py`
  - `SqliteDict(..., tablename="jobs")` etc.
  - Additional SQL table: `job_library` + indexes.
  - Acts as the **canonical job metadata store**.
- `src/anime_v2/api/models.py`
  - Auth tables in a separate DB (`auth.db`) under state dir.
- `src/anime_v2/library/queries.py`
  - Reads `job_library` via direct `sqlite3.connect` against the JobStore DB path.

No SQLModel/SQLAlchemy/Alembic found in the current codebase; DB is “raw sqlite3 + sqlitedict”.

### 3.2 Queue / Redis usage (current)
- “Queue” today is **in-process**:
  - `src/anime_v2/jobs/queue.py` uses `asyncio.Queue` and worker tasks.
- “Redis” is currently **optional and best-effort**:
  - `src/anime_v2/runtime/scheduler.py`: `_RedisMutex` (reduces dispatch stampede).
  - `src/anime_v2/utils/ratelimit.py`: Redis-backed token bucket, falls back to in-proc dict.
- There is **no Redis-backed durable job queue** yet.

This is good: we can introduce Level 2 by **extending the existing scheduler/queue/store** rather than adding a parallel job system.

---

## 4) Level 2 design: Redis queue + SQLite metadata, with fallback to Level 1

### 4.1 Definitions
- **Level 1 (L1)**: current behavior.
  - Single node, in-process queue (`JobQueue`) + SQLite metadata (`JobStore`) + restart recovery by scanning SQLite.
- **Level 2 (L2)**: optional Redis-backed distributed queue with SQLite metadata as the source of truth.
  - Multiple instances may run concurrently.
  - Redis provides “who should run what next”; SQLite provides “what is the job and what happened”.
  - If Redis is unavailable at boot or becomes unavailable during runtime, the system **falls back to L1** automatically.

### 4.2 Constraints and how we meet them
- **No duplicate systems**:
  - Do not create a second job store: extend `JobStore` (same SQLite file) with queue metadata tables.
  - Do not create a second executor: keep `JobQueue` as the executor; add a backend for “where job IDs come from”.
  - Do not create a second scheduler: keep `Scheduler` as the local backpressure/cap controller; integrate Redis intake.
- **Do not break existing functionality**:
  - Default behavior remains L1 unless explicitly enabled or unless Redis is configured and passes health checks.
  - If L2 cannot be established, the server still boots and runs jobs (L1).

### 4.3 Proposed module design (single canonical queue abstraction)
Add a small abstraction layer that plugs into the **existing** `Scheduler` + `JobQueue`:

- **`JobStore` remains source of truth** for job records and status.
- Introduce a **single interface** (names illustrative):
  - `JobDispatchBackend`:
    - `submit(job_id, priority, available_at, metadata)` (API → queue)
    - `claim(consumer_id, max_n, visibility_timeout_s)` (worker/dispatcher → job IDs)
    - `ack(job_id, claim_token)` (job finished / no longer needs to be retried)
    - `nack(job_id, claim_token, delay_s)` (retry later)
    - `health()` (used to decide fallback)

Implementations:
- **`InProcDispatchBackend`**: wraps current L1 behavior (enqueue into existing in-proc scheduler/queue).
- **`RedisDispatchBackend`**: uses Redis data structures + SQLite metadata to provide:
  - distributed claiming
  - crash safety via re-delivery
  - idempotent execution via SQLite leases

Where to put it (to avoid parallel systems):
- Place the interface and both implementations inside **the existing queue/scheduler namespace**:
  - preferred: `src/anime_v2/runtime/dispatch.py` (used by `Scheduler` / `server.py`)
  - alternative: `src/anime_v2/jobs/dispatch.py` (if you want “jobs owns dispatch”)

### 4.4 Redis structures (keys, types, consumer model)
Use Redis Streams for durable fan-out with consumer groups (good fit for “work items with ack/claim”):

**Key prefix (target):** `dubbing_pipeline:` (or `dp:`)

Keys:
- `dp:jobs:stream` (STREAM)
  - entries: `{job_id, priority, created_at, requested_mode, owner_id, schema_ver}`
- consumer group: `dp:jobs:workers`
  - consumer name: `<hostname>:<pid>` (or a stable instance ID)

Ack/reclaim:
- Workers `XREADGROUP GROUP dp:jobs:workers <consumer> COUNT N BLOCK 1000 STREAMS dp:jobs:stream >`
- On crash, other workers reclaim via `XAUTOCLAIM` with `min-idle-time` (visibility timeout).

Optional supporting keys (only if needed):
- `dp:jobs:cancel` (SET) or `dp:jobs:cancel:<job_id>` (STRING with TTL) for fast cancel signal propagation.
  - Note: cancellation already exists in SQLite + in-process; Redis cancel is an optimization, not a second source of truth.

### 4.5 SQLite schema additions (in the existing jobs DB)
Extend `JobStore` to create/migrate these tables in the same DB file it already manages (`jobs.db`):

1) `queue_outbox` (outbox pattern; prevents “job created in SQLite but never enqueued to Redis”)
- Columns:
  - `job_id TEXT PRIMARY KEY`
  - `state TEXT NOT NULL` (`pending`, `sent`, `sent_l1`, `error`)
  - `attempts INTEGER NOT NULL DEFAULT 0`
  - `last_error TEXT`
  - `created_at TEXT`, `updated_at TEXT`
  - `redis_entry_id TEXT` (optional)
- Flow:
  - on job creation: insert row as `pending`
  - dispatcher loop flushes pending rows to Redis; marks `sent`
  - if Redis down: mark `sent_l1` and push to in-proc queue (fallback)

2) `job_leases` (idempotency for distributed execution; prevents double-runs)
- Columns:
  - `job_id TEXT PRIMARY KEY`
  - `lease_owner TEXT NOT NULL` (consumer id)
  - `lease_expires_at INTEGER NOT NULL` (unix seconds)
  - `updated_at TEXT`
- Lease acquire logic:
  - atomic “insert-or-update if expired” (single SQL statement with WHERE).
  - if lease cannot be acquired, the worker acks the Redis message (or nacks with short delay) and moves on.

3) (Optional) `queue_events` (audit/debug only; can be skipped to keep DB small)
- Append-only rows for: submitted, claimed, started, finished, failed, canceled, retried.

Why SQLite tables (not a new DB):
- Reuses `JobStore` DB which is already the canonical job metadata store.
- Supports migration and downgrade without adding a new subsystem.

### 4.6 Fallback behavior (Redis down → Level 1)
Fallback needs to work in these cases:

1) **Redis not configured** (`REDIS_URL` unset):
- System stays L1.

2) **Redis configured but unreachable at boot**:
- Log warning and stay L1.
- Continue using existing restart recovery scanning of `JobStore`.

3) **Redis becomes unavailable mid-run**:
- Continue running any already-started jobs (executor is local).
- For new jobs:
  - Insert into `queue_outbox` as usual.
  - Dispatcher flush attempts fail; after a threshold (e.g., 1–2 attempts), route to L1 enqueue (`sent_l1`).
  - Emit metrics and an audit event (so operators know they’re in degraded mode).

4) **Redis recovers after fallback**:
- Do not “thrash” back and forth.
- Use a hysteresis strategy:
  - require N consecutive successful health checks over T seconds before switching back to L2.
  - when switching back, only new jobs go to Redis; L1 queue drains naturally.

### 4.7 Execution flow (end-to-end)
**Job submission (API/UI)**
1. Validate request, enforce auth/policy.
2. Create job record in `JobStore` (current behavior).
3. Insert/Upsert `queue_outbox(job_id, pending)` (new).
4. Return job_id immediately (do not block on Redis).

**Dispatcher loop (runs in server lifespan)**
1. Poll `queue_outbox WHERE state='pending' LIMIT k`.
2. If Redis healthy:
   - `XADD dp:jobs:stream * job_id <id> ...`
   - Mark outbox row `sent` + store entry id.
3. If Redis unhealthy:
   - Enqueue to in-proc queue (existing `JobQueue.enqueue` or `Scheduler.submit` path).
   - Mark outbox row `sent_l1`.

**Worker claim (Redis consumer)**
1. Read messages from stream group.
2. For each job_id:
   - Check if job exists and is still runnable (`JobStore.get`).
   - Acquire SQLite lease in `job_leases`:
     - if acquired: proceed to schedule/enqueue locally (existing path)
     - else: ack/nack and skip.

**Completion**
- Existing code updates job state in `JobStore`. Add:
  - clear lease row (or update expiry to now)
  - `XACK` stream entry

This keeps **SQLite as the single source of truth** and makes Redis a replaceable transport.

---

## 5) Tailscale golden-path docs + run scripts

### 5.1 Doc outline: `docs/GOLDEN_PATH_TAILSCALE.md`
This doc should be the single “happy path” and link out to deeper docs.

Proposed outline:
- **What this is** (private remote access for mobile, no port forwarding)
- **Prereqs**
  - Tailscale installed on server + phone
  - repo running mode (local or docker)
  - note about HTTPS not required inside tailnet (but auth still required)
- **Step 1: Start the server in Tailscale mode**
  - env vars: `REMOTE_ACCESS_MODE=tailscale`, `HOST=0.0.0.0`, `PORT=8000`
  - optional: `COOKIE_SECURE=0` if not behind HTTPS (explain tradeoffs)
- **Step 2: Verify allowlist is correct**
  - explain default includes `100.64.0.0/10` (tailscale CGNAT)
  - mention `ALLOWED_SUBNETS` override
- **Step 3: Get the phone URL**
  - run script prints `http://<tailscale-ip>:<port>/ui/login`
- **Step 4: Login + basic workflow**
  - submit a job (upload wizard)
  - view job detail
- **Step 5: Troubleshooting (short)**
  - 403 Forbidden reasons
  - tailscale not connected
  - wrong IP (LAN vs tailscale)
- **Links**
  - `docs/remote_access.md` (full)
  - `docs/WEB_MOBILE.md`
  - `docs/TROUBLESHOOTING.md`

### 5.2 Run scripts (what to add)
Goal: zero-guesswork commands that work for both local and docker users.

Add scripts under `scripts/remote/`:

1) `scripts/remote/golden_path_tailscale_start.sh`
- Exports the minimal env vars (REMOTE_ACCESS_MODE/HOST/PORT).
- Starts the server (foreground) using the new CLI entrypoint (`dubbing-pipeline-web`).
- Prints next-step command to run the check script.

2) `scripts/remote/golden_path_tailscale_check.py`
- Rename/extend existing `tailscale_check.py`.
- Checks:
  - tailscale installed + logged in
  - prints tailscale IPs
  - prints exact phone URL (`/ui/login`)
  - prints current `REMOTE_ACCESS_MODE` and server bind host/port from settings

3) `scripts/remote/golden_path_tailscale_docker.sh` (optional but high value)
- Runs `docker compose` with the recommended profile for internal-only exposure.
- Sets `REMOTE_ACCESS_MODE=tailscale` and binds ports appropriately.

These scripts should be referenced by the golden-path doc as the primary method.

---

## 6) Migration plan (do not break; no duplicates)

This is a staged plan so existing installs keep working while we converge on the final renamed repo.

### 6.1 Phase A — Introduce Level 2 queue behind flags (no behavior change by default)
- Implement the queue abstraction (§4.3) but default to Level 1.
- Add config flags (names below are targets; during migration accept both):
  - `QUEUE_BACKEND=auto|l1|redis` (default: `auto`)
  - `REDIS_QUEUE_STREAM_KEY` (default: `dp:jobs:stream`)
  - `REDIS_QUEUE_GROUP` (default: `dp:jobs:workers`)
  - `REDIS_QUEUE_VISIBILITY_TIMEOUT_S` (default: 300)
  - `REDIS_QUEUE_HEALTHCHECK_SEC` (default: 2)
- Extend `JobStore` to create queue metadata tables (outbox + leases).
- Wire in `server.py` lifespan:
  - start dispatcher task
  - if redis healthy and enabled, start consumer/claim loop
  - else use current boot recovery + in-proc queue

### 6.2 Phase B — Turn on Redis queue in CI/staging and validate fallback
- Add a CI job (or an opt-in CI matrix) that starts Redis and runs:
  - submit job, ensure it is claimed, lease acquired, processed, acked
  - simulate Redis down (stop container) and ensure enqueue routes to L1 without crashing
  - ensure no duplicate execution via leases

### 6.3 Phase C — Rename with compatibility shims (avoid breaking imports/scripts)
Key principle: **introduce new names first, keep old names as thin wrappers**, then remove old names in the final phase.

Steps:
1. Create new package directory `src/dubbing_pipeline/` and move code from `src/anime_v2/`.
2. Keep a temporary compatibility package `src/anime_v2/` that:
   - re-exports from `dubbing_pipeline`
   - emits `DeprecationWarning` on import (optional)
3. Repeat for legacy:
   - move `src/anime_v1/` to `src/dubbing_pipeline_legacy/`
   - keep temporary `src/anime_v1/` shim (temporary)
4. Update `pyproject.toml`:
   - new `project.name`
   - new console scripts
   - keep old console scripts mapped to new entrypoints temporarily
5. Update env var aliases:
   - during transition, accept both old and new env var names (e.g., `DUBBING_PIPELINE_OUTPUT_DIR` and `ANIME_V2_OUTPUT_DIR`).

### 6.4 Phase D — Final cut (remove all “anime/v1/v2/alpha” references)
Once consumers have migrated:
- Remove compatibility shims (`anime_v2`, `anime_v1`).
- Remove old console scripts (`anime-v2`, `anime-v2-web`, `anime-v1`).
- Remove old env var aliases (`ANIME_V2_*`, `V1_*`).
- Remove leftover strings:
  - redis keys, thread names, cache filenames, user-agent strings.
- Update docs/compose/README to only reference “Dubbing Pipeline”.

This phase is what achieves the “no anime/v1/v2/alpha references” requirement.

---

## 7) Risks and mitigations

### 7.1 Repo-wide rename risks
- **Risk**: import-path breakage and partial renames.
  - **Mitigation**: do the rename in phases with shims; add a CI check that fails on remaining `anime_`/`anime-v`/`\bv1\b`/`\bv2\b`/`alpha` strings after Phase D.
- **Risk**: users relying on old env vars or scripts.
  - **Mitigation**: dual-read env vars during Phase C; keep old console scripts as wrappers for one release window; emit warnings.
- **Risk**: docker compose and deploy docs drift.
  - **Mitigation**: update compose + README in the same PR as script renames; add `scripts/verify_env.py` checks for both old and new names during migration.

### 7.2 Redis queue risks
- **Risk**: duplicate job execution across instances (classic distributed queue hazard).
  - **Mitigation**: SQLite `job_leases` with atomic acquire; treat SQLite as authority.
- **Risk**: lost jobs during “SQLite commit succeeded but Redis enqueue failed” window.
  - **Mitigation**: outbox table flushed by a background dispatcher.
- **Risk**: Redis outage causes job submission failures.
  - **Mitigation**: never block submission on Redis; fallback to L1 enqueue; mark degraded mode via metrics/audit logs.
- **Risk**: “thrashing” between L2 and L1 as Redis flaps.
  - **Mitigation**: hysteresis policy before switching back to L2.

### 7.3 Tailscale golden path risks
- **Risk**: confusion between LAN IP and Tailscale IP leads to 403.
  - **Mitigation**: check script prints the correct URL and prints the effective allowlist mode; doc emphasizes “use the Tailscale IP”.

---

## 8) Deliverables checklist (what “done” means)

### 8.1 Tailscale golden path
- `docs/GOLDEN_PATH_TAILSCALE.md` added and referenced from `README.md` + `docs/remote_access.md`.
- `scripts/remote/golden_path_tailscale_check.py` prints:
  - bind host/port
  - remote access mode
  - tailscale IPs
  - phone URL (`/ui/login`)
- `scripts/remote/golden_path_tailscale_start.sh` starts server with correct env vars.

### 8.2 Redis queue + SQLite metadata (L2)
- New queue backend abstraction integrated into the existing scheduler/queue/store.
- `jobs.db` migrations add outbox + leases (and optional events).
- Redis stream keys and consumer group documented.
- Fallback behavior verified (Redis down at boot and mid-run).

### 8.3 Rename to “Dubbing Pipeline”
- Final tree has no:
  - `anime` / `anime_v*` / `anime-v*`
  - `v1` / `v2` tokens in names (except unavoidable semantic uses like “Tier‑1” if it matches search; adjust wording if needed)
  - `alpha`
- Packaging name, console scripts, docker image examples, env vars, docs all updated.
- Migration path shipped and then removed in the final cut.

