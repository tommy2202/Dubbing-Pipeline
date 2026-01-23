## Scale path (optional)

This project defaults to the local, single-writer SQLite-backed store and the in-process queue
(scheduler + job worker). You can opt into Redis for the queue without changing the default
behavior.

### When to use Redis

Use Redis when you need:

- Multiple workers on different machines.
- Durable queue state with crash recovery.
- Global concurrency caps across multiple processes.

Local mode remains the safest default for single-machine deployments.

### Enable Redis queue (optional)

1) Start Redis

```yaml
# docker-compose snippet
version: "3.8"
services:
  redis:
    image: redis:7
    ports:
      - "6379:6379"
    command: ["redis-server", "--appendonly", "yes"]
    volumes:
      - redis_data:/data
volumes:
  redis_data:
```

2) Configure environment variables

```bash
export REDIS_URL="redis://localhost:6379/0"
export QUEUE_BACKEND="redis"   # local|redis
```

Behavior:

- If Redis is unavailable, the server will fall back to the local queue.
- If `QUEUE_BACKEND` is unset, the existing `QUEUE_MODE=auto` behavior applies.

### Store backend (optional)

```bash
export STORE_BACKEND="postgres"   # local|postgres
export POSTGRES_DSN="postgresql://user:pass@host:5432/dbname"
```

Notes:

- The current release keeps the local store if Postgres is not configured or unavailable.
- Postgres store support is a placeholder in this release; use local mode unless you
  have added a PostgresStore implementation.

### Safe fallback

- If Redis/Postgres is not configured, the server starts in local mode.
- No changes are required for existing deployments.
