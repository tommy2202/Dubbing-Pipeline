# Scaling (Optional Single-Writer Mode)

This repo defaults to a simple, file-backed metadata store (SQLite + JSON files). For
most single-node deployments this is sufficient. If you want multiple processes but
keep the existing storage layer, you can enable an **optional single-writer mode**.

## When you need this

- You run multiple API workers or background processes and want to avoid concurrent
  metadata writes.
- You are **not** ready to switch to a full DB backend, but need a safe scaling step.

## How to enable (optional)

Set the following environment variables:

```bash
export SINGLE_WRITER_MODE=1
export SINGLE_WRITER_ROLE=writer   # only one process should be writer
# optional, lock file on shared filesystem (recommended)
export SINGLE_WRITER_LOCK_PATH=/path/to/shared/metadata.lock
```

For **read-only** replicas:

```bash
export SINGLE_WRITER_MODE=1
export SINGLE_WRITER_ROLE=reader
```

Behavior:
- **Writer**: acquires a filesystem lock on each metadata write.
- **Reader**: any metadata write attempt returns **HTTP 503** (read-only).
- **Default** (no env): unchanged behavior.

## Running multi-worker safely

1. Run exactly **one** writer process that handles all write endpoints
   (login, uploads, job submit/cancel, settings, voice store changes).
2. Run any additional API processes in **reader** mode and route only **GET**
   traffic to them (e.g., via load balancer rules).
3. Run **only one job worker** in writer mode (other workers should be disabled or
   read-only).

## Limitations

- No automatic request routing is provided; you must route write traffic to the writer.
- Reader processes will reject writes with 503 (by design).
- The lock file must be on a shared filesystem if multiple hosts are involved.
- Start the writer first so the SQLite/JSON files are initialized before readers.

## Verify

```bash
python3 scripts/verify_single_writer_or_db_backend.py
```

This script checks that writer mode allows writes and reader mode blocks them.
