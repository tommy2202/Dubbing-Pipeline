# Done: reliability + queue + golden path alignment

This document summarizes the current “golden path” for running **Dubbing Pipeline** safely, plus how the queue behaves and how to verify common failure modes.

## Golden path (Tailscale)

### Option A (recommended): Docker Compose (golden path)

From the repo root:

```bash
docker compose -f docker/compose.golden_path.tailscale.yml up -d --build
```

Then open the UI from your phone:

```bash
python3 scripts/remote/tailscale_check.py
```

It prints a URL like:
- `http://<tailscale-ip>:8000/ui/login`

Notes:
- The compose file runs `dubbing-web` with `REMOTE_ACCESS_MODE=tailscale` and binds `0.0.0.0:8000`.
- Outputs/logs are mounted to `./Output`, `./logs`, and state DBs live under `./Output/_state`.

### Option B: run directly on host (no Docker)

From the repo root:

```bash
export REMOTE_ACCESS_MODE=tailscale
export HOST=0.0.0.0
export PORT=8000
dubbing-web
```

Then:

```bash
python3 scripts/remote/tailscale_check.py
```

## Queue model (Redis + fallback)

There are two layers:

- **Level 2 (Redis)**: `src/dubbing_pipeline/queue/redis_queue.py`
  - Redis holds:
    - pending and delayed queues
    - per-job distributed locks
    - per-user running counters (for dispatch caps)
  - The server claims jobs from Redis and enqueues job IDs into the local executor.

- **Level 1 (fallback local)**: `src/dubbing_pipeline/queue/fallback_local_queue.py`
  - Used when Redis is not configured or not reachable.
  - SQLite remains the source of truth for job metadata/state; the fallback loop re-submits queued jobs into the scheduler.

### How selection works

- `QUEUE_MODE=auto` (default):
  - use Redis when `REDIS_URL` is configured and reachable
  - otherwise fall back to local
- `QUEUE_MODE=redis`: force Redis (refuses to start new work if Redis is down)
- `QUEUE_MODE=fallback`: force local fallback

### What “works” means (practical behavior)

- Redis mode:
  - jobs are queued in Redis and claimed in priority order
  - per-user running caps are enforced at dispatch-time
  - locks prevent the same job running on multiple workers
- Fallback mode:
  - jobs are dispatched locally using the in-process scheduler/executor
  - no distributed locking; intended for single-instance use

## Renamed paths and commands (current)

- **Python package**: `dubbing_pipeline` (under `src/dubbing_pipeline/`)
- **Web entrypoint**: `dubbing-web`
- **CLI**: `dub` (user-facing help text refers to “Dubbing Pipeline”)
- **Key env vars**:
  - `DUBBING_OUTPUT_DIR`, `DUBBING_LOG_DIR`, `DUBBING_STATE_DIR`
  - `REDIS_URL`, `QUEUE_MODE`

## Verifiers and “correctness” scripts

Run these from the repo root:

```bash
python3 scripts/show_config.py --no-env-file --no-secrets-file
python3 scripts/verify_queue_fallback.py
python3 scripts/verify_queue_redis.py
python3 scripts/e2e_concurrency_two_users.py
python3 scripts/verify_no_anime_or_versions.py
```

Notes:
- `verify_queue_redis.py` will use:
  - `REDIS_URL` if set, otherwise it tries Docker (`redis:7-alpine`), otherwise it tries `redis-server` if installed.

## Known limitations

- **Multi-instance without Redis is not supported**: fallback mode is single-instance by design.
- **Heavy model downloads are not required for the “lite” verifiers**, but full processing jobs will require optional ML deps and (depending on settings) model downloads.
- **GPU checks** are environment-dependent and skip cleanly on CPU-only machines.

## Troubleshooting (links)

- Remote access / Tailscale: `docs/remote_access.md` and `docs/GOLDEN_PATH_TAILSCALE.md`
- Common operational issues: `docs/TROUBLESHOOTING.md`
- Reliability test scenarios: `docs/RELIABILITY_TESTS.md`

