# Concurrency and Single-Writer Model

This project uses SQLite-backed stores (SqliteDict tables and raw SQLite tables) for
jobs, library metadata, and auth state. SQLite permits concurrent readers, but only
one writer at a time. To prevent multi-process write races and `database is locked`
errors, all write operations are protected by a cross-process file lock.

## Recommended deployment (v0 hardening)

**Single writer process:**

- Run **one** API/worker process that performs writes to:
  - Job store (jobs, uploads, idempotency, presets, projects)
  - Library metadata tables inside the job DB
  - Auth store (users, API keys, refresh tokens, QR login, recovery codes)

**Additional workers:**

- You may run additional processes in **read-only** mode for UI/list operations.
- Avoid multi-process writes to the same SQLite DBs.

## Safe multi-worker guidance

If multiple processes must be running:

1. Designate exactly **one writer** process.
2. All other processes must be **read-only** for SQLite-backed stores.
3. Use external queues/services for write-heavy workflows if needed.

## Implementation notes

- Cross-process file locks are created alongside each SQLite DB file
  (e.g., `jobs.db.lock`, `auth.db.lock`).
- Reads are not locked and remain concurrent.
