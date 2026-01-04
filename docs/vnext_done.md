## vNext hardening: done (A–M)

This repo now includes a single canonical hardened v2 implementation for mobile + remote use, plus the vNext “Security + Privacy + Ops + UX” features.

### What was added (high level)
- **Remote access (opt-in)**: Tailscale primary; Cloudflare Tunnel + Access optional.
- **Auth**: username/password (argon2), JWT access + rotating refresh tokens, cookie sessions + CSRF, strict CORS, rate limiting, audit logging.
- **Safer UX**: QR login, session/device management, optional admin TOTP.
- **RBAC + API keys**: roles (`viewer/operator/editor/admin`) + scoped API keys.
- **Jobs**: bounded queue + progress/status + cancel/kill; worker isolation with stage timeouts and cooperative cancellation.
- **File safety**: allowlists, upload limits, ffprobe validation, traversal protection.
- **Privacy + encryption**: optional AES-GCM encryption at rest; privacy mode/minimal artifacts + retention automation.
- **Audit + secret hygiene**: append-only security audit logs + secret redaction in structured logs.
- **Notifications**: optional private self-hosted `ntfy` job-finish notifications.
- **Playback**: mobile-friendly MP4 + optional HLS + “Open in VLC” links; master MKV preserved.
- **Library**: search/filters/tags/archive/delete.
- **Imports**: external SRT/JSON import to skip ASR/translation when provided.
- **Models**: model cache/disk status + optional prewarm (default-off).

### Default-off guarantees (safe by default)
- **Remote access**: `REMOTE_ACCESS_MODE=off` unless you explicitly enable it.
- **Legacy token login**: `ALLOW_LEGACY_TOKEN_LOGIN=0` unless explicitly enabled.
- **Encryption at rest**: `ENCRYPT_AT_REST=0` unless configured (and fails safe if key missing/invalid).
- **Privacy mode**: off unless per-job enabled.
- **Notifications**: `NTFY_ENABLED=0` unless configured.
- **Model downloads**: disabled unless `ENABLE_MODEL_DOWNLOADS=1` and egress is allowed.
- **Proxy trust**: forwarded headers are only trusted in Cloudflare mode.

### How to enable each safely (links)
- **Remote mobile**: `docs/mobile_remote.md` (details: `docs/remote_access.md`)
- **Mobile workflow**: `docs/mobile_update.md`
- **Notifications (ntfy)**: `docs/notifications.md`
- **Security overview**: `docs/security.md`
- **Library management**: `docs/library.md`
- **Model management**: `docs/models.md`

### Quick test commands (<= 8)
```bash
python3 scripts/verify_env.py
python3 scripts/verify_auth_flow.py
python3 scripts/verify_qr_login.py
python3 scripts/security_file_smoke.py
python3 scripts/verify_job_submission.py
python3 scripts/verify_playback_variants.py
python3 scripts/verify_ntfy.py
python3 scripts/security_mobile_gate.py
```

