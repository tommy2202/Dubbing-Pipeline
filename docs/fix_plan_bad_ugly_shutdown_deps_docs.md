# Fix plan: bad/ugly issues + shutdown + deps + docs

## Repo scan inventory (startup/shutdown lifecycle)
- `src/dubbing_pipeline/server.py`
  - FastAPI lifespan boot: JobStore, JobQueue, Scheduler thread, AutoQueueBackend, prune loop task, ModelManager prewarm.
  - Shutdown: begin draining, stop scheduler, stop queue backend, graceful shutdown of JobQueue, cancel prune task.
  - Signal handlers trigger draining.
- `src/dubbing_pipeline/runtime/lifecycle.py`
  - Global draining flag + deadline used by readiness checks and submit gates.
- `src/dubbing_pipeline/runtime/scheduler.py`
  - Background scheduler thread dispatch loop.
- `src/dubbing_pipeline/jobs/queue.py`
  - Worker tasks (`asyncio.create_task`), graceful shutdown via `Queue.join()` + cancellation.
- `src/dubbing_pipeline/queue/manager.py`
  - AutoQueueBackend monitor loop task.
- `src/dubbing_pipeline/queue/redis_queue.py`
  - Health/consume/delayed mover tasks, plus per-job lock refresh tasks.
- `src/dubbing_pipeline/queue/fallback_local_queue.py`
  - Local scan loop task.
- `src/dubbing_pipeline/web/routes_jobs.py`
  - SSE generators for job events/log streaming.
- `src/dubbing_pipeline/web/routes_webrtc.py`
  - Per-peer idle watcher tasks (not centrally tracked today).

## Dependency conflict points (fresh-machine risk)
- `pyproject.toml` uses lower bounds only; no constraints file for local installs.
- `docker/constraints.txt` pins FastAPI, uvicorn, pydantic, sse-starlette, numpy, etc but is not used by local installs.
- Docker installs pinned torch separately; local installs do not pin torch or numpy (numpy can float to 2.x).
- README note about Docker numpy pin does not match `docker/constraints.txt` (1.22.0 vs 1.26.4).

## Release hygiene issues (bad/ugly carryover)
- `docs/repo_cleanup_plan.md` marks `data/reports/**` and `voices/embeddings/Speaker1.npy` as unresolved tracked artifacts.
- Guardrail scripts do not explicitly block these paths today.

## Complexity hotspots (docs/setup sprawl)
- Setup docs overlap across `README.md`, `docs/SETUP.md`, `docs/FRESH_MACHINE_SETUP.md`,
  `docs/WEB_MOBILE.md`, `docs/GOLDEN_PATH_TAILSCALE.md`, `docs/mobile_remote.md`,
  `docs/remote_access.md`, and `README-deploy.md`.
- Legacy docs (e.g., `docs/mobile_update.md`) add noise for new users.

## Missing/underwired E2E reliability tests
- `scripts/e2e_concurrency_two_users.py` exists but is not referenced in `docs/RELIABILITY_TESTS.md`
  and is not run in `.github/workflows/ci-core.yml` nightly reliability job.

---

## Plan of record (exact changes to implement)

### 1) Shutdown fix (TestClient CancelledError + background drain)
Files to modify:
- `src/dubbing_pipeline/server.py`
  - Replace `suppress(Exception)` with `suppress(asyncio.CancelledError, Exception)` for:
    - `queue_backend.stop()`
    - `_prune_task` cancellation/await
    - any other awaited background tasks in shutdown
  - Ensure shutdown order drains queue backend and JobQueue before stopping Scheduler (prevents new enqueues during drain).
  - Call a new WebRTC cleanup helper on shutdown.
- `src/dubbing_pipeline/queue/manager.py`
  - In `AutoQueueBackend.stop`, suppress `asyncio.CancelledError` when awaiting `_task`.
- `src/dubbing_pipeline/web/routes_webrtc.py`
  - Track idle watch tasks in a module-level registry.
  - Add `shutdown_webrtc_peers()` that closes peers and cancels idle watch tasks.
  - Call this from server shutdown (via import or `app.state` callback).
- `src/dubbing_pipeline/web/routes_jobs.py`
  - Wrap SSE generators with `try/except asyncio.CancelledError` and exit early when `lifecycle.is_draining()`.

Tests to add/update:
- Add `tests/test_shutdown_clean.py` (or extend `tests/test_draining.py`) to assert
  `TestClient(app)` context exit is clean after a minimal request.
- Add a focused unit test to ensure `AutoQueueBackend.stop` swallows `CancelledError`.

Why: Prevent CancelledError propagation during TestClient shutdown and ensure background tasks drain cleanly.

### 2) Dependency pins/constraints (fresh-machine install stability)
Files to add/modify:
- Add `constraints/py310.txt` (or move `docker/constraints.txt` there) as the canonical pins file.
- Update `docker/Dockerfile` and `docker/Dockerfile.cuda` to COPY the canonical constraints path.
- Update `pyproject.toml` only if needed for explicit `starlette` pin in constraints docs.
- Update install docs to use constraints:
  - `README.md`
  - `docs/SETUP.md`
  - `docs/FRESH_MACHINE_SETUP.md`
- Fix README Docker numpy pin note to match constraints.

Optional guardrail:
- Add `scripts/verify_constraints.py` or extend `scripts/verify_env.py` to compare installed versions
  against `constraints/py310.txt`.

Why: Keep FastAPI/Starlette/sse-starlette and numpy/ML deps on known-good versions across new machines.

### 3) README fresh-machine path (CLI + web + mobile upload)
Files to modify:
- `README.md`
  - Add "Clean machine quickstart" with:
    - Core install using constraints
    - CLI run example
    - Web UI run example
    - Link to mobile upload flow in `docs/WEB_MOBILE.md`
    - Link to clean setup guide in `docs/FRESH_MACHINE_SETUP.md`
  - Fix Docker numpy pin note.

Why: Provide a single obvious entry point for new machines covering CLI + web + mobile upload.

### 4) Clean computer setup guide (core + optional + mobile + remote)
Files to modify:
- `docs/FRESH_MACHINE_SETUP.md`
  - Expand into a detailed "clean computer setup" guide:
    - Core install with constraints
    - Optional feature packs (translation/diarization/tts/mixing/webrtc)
    - Web UI + mobile upload workflow (Upload Wizard + resumable chunks)
    - Remote job submission (Tailscale recommended, Cloudflare optional)
  - Keep a short CI parity section at the end.

Why: Consolidate the golden path and reduce doc fragmentation.

### 5) Release hygiene cleanup (bad/ugly carryover)
Files to modify:
- Remove or relocate tracked artifacts:
  - `data/reports/**`
  - `voices/embeddings/Speaker1.npy`
- Update `.gitignore` to explicitly ignore `data/reports/` and `voices/embeddings/` if they should never be tracked.
- Update guardrails:
  - `scripts/check_no_tracked_artifacts.py` to flag these paths.
- Update status docs:
  - `docs/repo_cleanup_plan.md`
  - `docs/RELEASE_CHECKLIST.md`

Why: Prevent artifact leakage and keep release zips clean.

### 6) E2E reliability test wiring
Files to modify:
- `docs/RELIABILITY_TESTS.md`
  - Add a "two-user concurrency" scenario and include the script in Quickstart.
- `.github/workflows/ci-core.yml`
  - Add `python scripts/e2e_concurrency_two_users.py` to `reliability_nightly` job (and optional manual runs).

Why: Ensure concurrency sanity checks are documented and executed in CI.

### 7) Bad/ugly duplicate logic fixes (from existing plans)
Files to modify (future work, keep surgical):
- `src/dubbing_pipeline/cli.py` and `src/dubbing_pipeline/jobs/queue.py`
  - Route mode defaults through `src/dubbing_pipeline/modes.py::resolve_effective_settings`.
- `src/dubbing_pipeline/web/routes_ui.py` and `src/dubbing_pipeline/web/routes_jobs.py`
  - Use a single canonical output discovery helper per `docs/library_full_plan.md`.

Why: Enforce "no duplicate systems" and reduce drift between CLI and server behavior.

---

## New scripts/tests/docs to add
- Test: `tests/test_shutdown_clean.py` (or extend `tests/test_draining.py`).
- Doc updates: `docs/FRESH_MACHINE_SETUP.md`, `docs/RELIABILITY_TESTS.md`, `README.md`.
- Optional script: `scripts/verify_constraints.py`.
