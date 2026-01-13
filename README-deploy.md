## Deployment (blessed paths)

This repo supports two recommended deployment options:

- **Public HTTPS** via **Caddy + Let’s Encrypt** (best for a dedicated host + your own domain)
- **Private remote access** via **Cloudflare Tunnel** (best for “no inbound ports”)

The app itself exposes health checks at:
- `GET /healthz` (liveness)
- `GET /readyz` (readiness)

### Shared prerequisites

- **Create `.env`** in repo root (copy from `.env.example`) and set at least:
  - `JWT_SECRET`, `SESSION_SECRET`, `CSRF_SECRET` (strong random values)
  - `ADMIN_USERNAME`, `ADMIN_PASSWORD` (optional bootstrap)
  - `COQUI_TOS_AGREED=1` (if you intend to use XTTS)
  - `CHAR_STORE_KEY` (required if you want persistent character IDs; 32-byte base64)

Generate `CHAR_STORE_KEY`:

```bash
python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
```

Set the container image:
- `GHCR_IMAGE=ghcr.io/<owner>/dubbing-pipeline:<tag>`

---

## 1) Public HTTPS behind Caddy (Let’s Encrypt)

### Requirements

- A public server with ports **80** and **443** reachable.
- A domain name pointing to the server (DNS A/AAAA record).

### Steps

1) Set these environment variables (shell env or `.env` used by compose):

- `DOMAIN=your.domain.example`
- `CADDY_EMAIL=you@example.com`
- `GHCR_IMAGE=ghcr.io/<owner>/dubbing-pipeline:<tag>`

2) Start services:

```bash
docker compose -f deploy/compose.public.yml up -d
```

3) Open:

- `https://your.domain.example/`

Caddy will automatically obtain and renew TLS certificates. The included `deploy/caddy/Caddyfile` enables **HSTS** and hardened response headers.

---

## 2) Private remote access (zero inbound ports) via Cloudflare Tunnel

This uses a **cloudflared sidecar** that creates an outbound-only tunnel. You do **not** open any ports on your host.

### Requirements

- A Cloudflare account + zone (for a stable hostname).
- A Cloudflare Tunnel token.

### Steps (stable hostname)

1) Create a tunnel in Cloudflare Zero Trust and map a hostname to your service:

- Service: `http://api:8080`
- Hostname: e.g. `dubbing-pipeline.yourdomain.example`

2) Copy the tunnel token and set:

- `CLOUDFLARE_TUNNEL_TOKEN=...`
- `GHCR_IMAGE=ghcr.io/<owner>/dubbing-pipeline:<tag>`

Optional:
- `CORS_ORIGINS=https://dubbing-pipeline.yourdomain.example`

3) Start services:

```bash
docker compose -f deploy/compose.tunnel.yml up -d
```

4) Your **stable remote URL** is the hostname you configured in Cloudflare (e.g. `https://dubbing-pipeline.yourdomain.example/`).

### Notes

- This path provides remote access without exposing inbound ports. Your host only makes outbound connections to Cloudflare.
- If you need a fully private mesh instead (no public hostname at all), we can add a **Tailscale sidecar** profile as an alternative.

