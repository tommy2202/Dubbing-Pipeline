## Private job notifications (self-hosted `ntfy`)

This repo supports **optional** job-finish notifications via a **private self-hosted** [`ntfy`](https://docs.ntfy.sh/) server.

### Goals / guarantees
- **Private by default**: the provided compose stack binds to **localhost only** (`127.0.0.1`) unless you change it.
- **Opt-in**: if `ntfy` isn’t configured, the pipeline runs normally (no failures).
- **No credential leaks**: app logs/audit logs never include ntfy credentials.

---

## Run `ntfy` privately (docker-compose)

### 1) Create config

Copy the template:

- `docker/ntfy/server.yml.example` → `docker/ntfy/server.yml`

Then start:

```bash
docker compose -f docker/ntfy/docker-compose.yml up -d
```

By default the host port is **localhost-only**:
- `http://127.0.0.1:8081`

### 2) Create an ntfy user (auth enabled, default deny)

The template enables `auth-file` and sets `auth-default-access: deny-all`.

Create a user:

```bash
docker compose -f docker/ntfy/docker-compose.yml exec ntfy ntfy user add <username>
```

Create an access token (recommended over passwords in apps):

```bash
docker compose -f docker/ntfy/docker-compose.yml exec ntfy ntfy token add <username>
```

Keep the token private.

---

## Recommended: Tailscale/private-network access (no public ports)

Option A (simplest, safest): keep ntfy bound to localhost and access it via the server itself only.

Option B (phone access over tailnet): bind ntfy to your **Tailscale IP**.

In `docker/ntfy/docker-compose.yml`, change:

- `127.0.0.1:8081:80` → `<tailscale-ip>:8081:80`

This exposes ntfy only on that interface (still not “public internet”).

Do **not** bind to `0.0.0.0` unless you fully understand the risks.

---

## Configure the dubbing server to send notifications

Add these settings (example):

### `.env` (public/non-secret)

```bash
NTFY_ENABLED=1
NTFY_BASE_URL=http://127.0.0.1:8081
NTFY_TOPIC=<random-topic>
# Optional: send a copy to an admin-only topic
# NTFY_NOTIFY_ADMIN=1
# NTFY_ADMIN_TOPIC=<admin-topic>
# Optional: for absolute click URLs in notifications
PUBLIC_BASE_URL=http://<tailscale-ip>:8000
```

### `.env.secrets` (secret)

Use an access token (recommended):

```bash
NTFY_AUTH=token:<your-ntfy-token>
```

Or user/password (works, but token is preferred):

```bash
NTFY_AUTH=userpass:<username>:<password>
```

---

## Phone setup

1) Install the **ntfy** app on your phone.
2) Set the server to your private ntfy base URL (localhost won’t work from phone; use Tailscale IP if you enabled Option B).
3) Subscribe to your topic (same value as `NTFY_TOPIC`).

---

## Per-user topics (recommended)

Each user can opt-in and set **their own topic**:

- Open `/ui/settings/notifications`
- Enable notifications
- Enter a topic (letters/numbers/`-`/`_`/`.`; max 64 chars)

This keeps notifications private per user (no shared channel).

Each user must set a topic when enabling notifications.

---

## Privacy mode behavior

When privacy mode is enabled for a job (or when the job uses **minimal retention**), notifications avoid including file names. They include only:
- job id
- status

---

## Security notes

- Notification auth tokens are **server-only** (`NTFY_AUTH` in `.env.secrets`).
- User settings store **no secrets** — only enable/disable + topic.
- Notifications never include transcripts or secret env values.

---

## Verify

This script is safe to run even if notifications are not configured:

```bash
python scripts/verify_ntfy.py
```

