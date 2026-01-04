## Security (vNext)

This repo’s hardened web/API stack is the **v2 server** (`src/anime_v2/server.py`). Remote access is **opt-in** and all advanced security/privacy/ops features are **default-off** unless configured.

### Auth + sessions (mobile-safe)
- **Primary**: username/password (argon2) + short-lived access token + rotating refresh token (server-side stored).
- **Browser sessions**: HttpOnly cookies; **CSRF enforced** for state-changing requests (double-submit via `csrf` cookie + `X-CSRF-Token` header).
- **Legacy token-in-URL**: **OFF by default**; only available when explicitly enabled and only on private/loopback networks.
- **QR login**: optional admin flow (single-use, short-lived nonce; no reusable secrets in the QR).
- **TOTP 2FA**: optional for admin; TOTP secret stored encrypted; recovery codes supported.

### Authorization (RBAC + scoped API keys)
- **Roles**: `viewer`, `operator`, `editor`, `admin`.
- **API keys**: scoped (e.g. `read:job`, `submit:job`, `edit:job`, `admin:*`), stored **hashed** server-side.

### Remote access (opt-in)
Remote mode is gated by allowlists and proxy-safe behavior:
- `REMOTE_ACCESS_MODE=off` (default): no special proxy trust; intended for local/LAN use.
- `REMOTE_ACCESS_MODE=tailscale`: allowlist private + Tailscale CGNAT (`100.64.0.0/10`).
- `REMOTE_ACCESS_MODE=cloudflare`: trust forwarded headers only in this mode; can validate Cloudflare Access JWT at origin.

See `docs/remote_access.md` (and `docs/mobile_remote.md` for the mobile workflow).

### File safety
- Chunked uploads enforce **size limits**, **allowlisted extensions/MIME**, and **ffprobe** validation before job acceptance.
- Server file picker is restricted to allowlisted base directories; directory traversal is blocked.

### Privacy mode + encryption at rest (optional)
- **Privacy mode**: per-job toggles for data minimization (no transcript storage, no source-audio retention, minimal artifacts).
- **Encryption at rest**: optional AES-GCM for sensitive artifacts; **fails safe** if misconfigured (won’t silently write plaintext while “enabled”).

### Audit logging + secret hygiene
- **Audit logs**: append-only JSONL security events (login/logout, uploads, job actions, edits/overrides, admin actions) with aggressive payload scrubbing.
- **Secret masking**: structured logs redact configured secret literals and common credential patterns.

### Quick verification
- End-to-end gate (recommended):

```bash
python3 scripts/security_mobile_gate.py
```

