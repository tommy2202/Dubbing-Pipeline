# Queue architecture

This document describes the single canonical queue path used by the system.

## Canonical entrypoint

All API/UI job submissions must go through:

- `app.state.queue_backend` (an `AutoQueueBackend` instance)
- `submit_job_or_503(...)` helper for consistent errors/logging

No web route should call `Scheduler.submit()` directly.

## AutoQueueBackend

`AutoQueueBackend` is the single source of truth for queue selection:

- If Redis is configured and healthy, it routes to `RedisQueue`.
- Otherwise it routes to `FallbackLocalQueue`.

This decision is made at runtime and can change if Redis health changes.

## RedisQueue

RedisQueue provides:

- distributed locks
- per-user counters/quotas
- durable pending/delayed queues

It claims jobs from Redis and forwards them into the local executor via the
queue backend callback.

## FallbackLocalQueue

FallbackLocalQueue is the local, single-node path. It is the **only** component
allowed to call `Scheduler.submit()` directly.

This keeps scheduler usage centralized and ensures all submissions remain
backend-agnostic.

## Worker execution

The JobQueue worker pulls job IDs and executes the pipeline. It calls queue
backend hooks (`before_job_run` / `after_job_run`) for lock and accounting
behavior, but does not make backend selection decisions.
