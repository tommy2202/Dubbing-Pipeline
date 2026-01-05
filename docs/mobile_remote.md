## Mobile remote usage (Tailscale primary, Cloudflare optional)

This document is the “phone-first” remote access guide. It builds on:
- `docs/mobile_update.md` (mobile UI workflow)
- `docs/remote_access.md` (full Tailscale + Cloudflare setup and security model)

### Recommended: Tailscale (private, no port forwarding)
1) Install Tailscale on the server machine and your phone (same tailnet).
2) Start the server:

```bash
export REMOTE_ACCESS_MODE=tailscale
export HOST=0.0.0.0
export PORT=8000
anime-v2-web
```

3) Get the exact URL to open on your phone:

```bash
python3 scripts/remote/tailscale_check.py
```

Open the printed URL (typically `http://<tailscale-ip>:8000/ui/login`).

### Optional: Cloudflare Tunnel + Access (browser URL)
Use this only if you need a normal HTTPS URL without installing Tailscale on the phone.

1) Start the app in Cloudflare mode:

```bash
export REMOTE_ACCESS_MODE=cloudflare
export TRUST_PROXY_HEADERS=1
export HOST=0.0.0.0
export PORT=8000
anime-v2-web
```

2) Follow `docs/remote_access.md` and `scripts/remote/cloudflared/README.md` to provision the tunnel and protect it with Access.

### Verify (synthetic)
```bash
python3 scripts/security_mobile_gate.py
```

