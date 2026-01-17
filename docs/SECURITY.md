## Security notes (runtime state & sensitive files)

### Where auth/session data is stored

The server stores **users, API keys, refresh-token metadata (hashed), QR login nonces, and recovery-code hashes** in a SQLite database.

- **Auth DB**: `auth.db`
- **Jobs DB** (may include user/job metadata): `jobs.db`

By default these are stored under a **runtime-only state directory**:

- **Default state dir**: `Output/_state/`
- **Default paths**:
  - `Output/_state/auth.db`
  - `Output/_state/jobs.db`

You can override the state directory (recommended for production) with:

- `DUBBING_STATE_DIR=/var/lib/dubbing_pipeline/state` (or another non-repo mount)

### Guardrails

- The server **refuses to boot** if it detects an unsafe DB location (e.g. under `build/`, `dist/`, `backups/`, any `_tmp*` directory, or inside the repo workspace outside the allowed runtime state dir).
- CI runs `scripts/check_no_sensitive_runtime_files.py` via `make check` to fail builds if:
  - `auth.db`/`jobs.db` appear in forbidden locations, or
  - any `.zip` under the repo contains `*.db` / `*.sqlite*`, or
  - any `*.db` / `*.sqlite*` is tracked by git.

### CORS + CSRF production hardening

Production requires an explicit allowlist for browser origins and secure cookies.

Set your allowlist with `CORS_ORIGINS` (comma-separated):

- Example (single UI origin):
  - `CORS_ORIGINS=https://ui.example.com`
- Example (multiple origins):
  - `CORS_ORIGINS=https://ui.example.com,https://admin.example.com`

**Do not** use `*` or wildcard patterns in production.

Cookie settings:

- `COOKIE_SECURE=1` in production.
- `COOKIE_SAMESITE=lax` for same-site UI (Tailscale, direct access).
- `COOKIE_SAMESITE=none` **only** when you must use cross-site cookies (tunnel + separate UI domain) and TLS is enabled.

### How to reset auth safely

Resetting auth deletes all users/sessions/API keys and forces re-bootstrap.

1) **Stop the server**
2) Delete the state DBs:
   - `rm -f Output/_state/auth.db Output/_state/jobs.db`
   - or if you use `DUBBING_STATE_DIR`, delete `"$DUBBING_STATE_DIR/auth.db"` and `"$DUBBING_STATE_DIR/jobs.db"`
3) Start the server again

If `ADMIN_USERNAME`/`ADMIN_PASSWORD` are set, the server will attempt to bootstrap an admin account on startup.

