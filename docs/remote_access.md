## Remote access (mobile data) — Tailscale-first (default) and Cloudflare Tunnel (optional)

This repo is **offline-first** and is designed to run safely without exposing your laptop publicly. The recommended way to use it from a phone on mobile data is **Tailscale** (private network). If you need a normal browser URL without installing Tailscale, use **Cloudflare Tunnel + Access**.

### Security model (recommended)
- **Preferred (default)**: `ACCESS_MODE=tailscale` (private-only; no public exposure)
- **Optional**: `ACCESS_MODE=tunnel` + **Cloudflare Access allowlist (Policy A)** + optional Access JWT validation at the origin
- **Avoid**: exposing `HOST=0.0.0.0` + port-forwarding without allowlists/auth

### Authentication (recommended)
- Use the built-in login UI at:
  - `/login` (alias) or `/ui/login` (canonical)
- For browser UI sessions:
  - enable “session cookie” login (default in the UI)
  - CSRF is required for state-changing requests
- **Legacy “token in URL” (`?token=...`) is OFF by default** and only available when:
  - `ALLOW_LEGACY_TOKEN_LOGIN=1` **and**
  - the client is on a private/loopback network
  - This is labeled unsafe on public networks and the UI shows a warning banner.

---

## Option A (primary): Tailscale

### 1) Install and log in
- Install Tailscale on:
  - the **server laptop** (where `dubbing-web` runs)
  - your **phone**
- Log in both devices to the same tailnet.

### 2) Start the server
Run the web server normally. The default bind is **127.0.0.1** for safety, so
set `HOST` to either your **Tailscale IP** or `0.0.0.0` if you want your phone
to connect directly over the tailnet.

```bash
export ACCESS_MODE=tailscale
export HOST=100.x.y.z   # or 0.0.0.0
export PORT=8000
dubbing-web
```

Notes:
- Binding to `0.0.0.0` is OK here because **requests are allowlisted** in `tailscale` mode.
- Default allowlist includes **LAN private ranges** + **Tailscale CGNAT** `100.64.0.0/10`.

### 3) Find your Tailscale IP and open from phone
Run:

```bash
python3 scripts/remote/tailscale_check.py
```

It prints the exact URL to open, typically:
- `http://<tailscale-ip>:8000/ui/login`

### 4) Common issues
- **Phone can’t reach server**:
  - confirm both devices are “Connected” in Tailscale
  - confirm server is listening on `0.0.0.0:8000`
- **Still blocked** (403):
  - If your tailnet uses IPv6-only or custom routing, set:

```bash
export ALLOWED_SUBNETS="100.64.0.0/10,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8,::1/128,fc00::/7"
```

---

## Option B (optional): Cloudflare Tunnel + Access

Use this when you want a normal HTTPS URL without installing Tailscale on the phone.

### 1) Start the app in Tunnel mode

```bash
export ACCESS_MODE=tunnel
export HOST=0.0.0.0
export PORT=8000
export TRUST_PROXY_HEADERS=1
export TRUSTED_PROXIES="127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
dubbing-web
```

Tunnel mode:
- still enforces an IP allowlist (defaults to loopback + common container/private nets; **does not allow generic LAN by default**)
- can optionally enforce **Cloudflare Access JWT** at the origin (recommended)
- if `TRUSTED_PROXIES` is empty, the app **warns loudly** and ignores forwarded headers

### 2) Configure Cloudflare Tunnel
This repo already includes a tunnel compose file:
- `deploy/compose.tunnel.yml`

You’ll need to provision the tunnel in Cloudflare and supply:
- `CLOUDFLARE_TUNNEL_TOKEN` (secret; put it in `.env.secrets`, not in git)

See the template and notes:
- `scripts/remote/cloudflared/config.yml`
- `scripts/remote/cloudflared/README.md`

Validate your local setup:

```bash
python3 scripts/remote/cloudflared/cloudflared_check.py
```

### 3) Protect with Cloudflare Access (Policy A)
Enable Access for the hostname (one-time PIN or OIDC). Use a **deny-by-default**
policy with an explicit allowlist.

**Policy A (allowlist):**
- **Include**: specific emails or an **Access Group** (preferred)
- **Session duration**: keep short (e.g., 8–12 hours) and require re-auth
- **Deny by default**: add a catch-all **Deny** policy below the allowlist

To additionally validate Access at the origin, set:

```bash
export CLOUDFLARE_ACCESS_TEAM_DOMAIN="your-team"
export CLOUDFLARE_ACCESS_AUD="your-access-app-aud"
```

The server will then require and verify the header:
- `Cf-Access-Jwt-Assertion: <jwt>`

### 4) Optional: PUBLIC_BASE_URL for deep links
If you want invite links, ntfy links, or UI deep links to use your public
hostname, set:

```bash
export PUBLIC_BASE_URL="https://your-app.example.com"
```

Notes:
- JWKS fetch uses egress to `https://<team>.cloudflareaccess.com/...`.
  - If you run with `ALLOW_EGRESS=0`, the server will only validate Access JWTs if it has a cached JWKS at `/tmp/dubbing_pipeline_cf_access_jwks.json`.

---

## Verification

Run the remote-mode verifier (no real media required):

```bash
python3 scripts/verify_remote_mode.py
```

It simulates allowed and disallowed client IPs and asserts the server returns 200/403 correctly.

### Job submission verification (no real content required)

```bash
python3 scripts/verify_job_submission.py
```

This verifies:
- resumable upload init/chunk/complete
- job creation referencing `upload_id`
- polling job status
- cancel job
- `/api/jobs/{id}/outputs` + `/api/jobs/{id}/logs` aliases

### Mobile playback outputs verification

```bash
python3 scripts/verify_mobile_outputs.py
```

This verifies:
- H.264/AAC mobile MP4 encoding
- optional HLS playlist + segments
- HTTP Range response headers (206 + `Accept-Ranges` + `Content-Range`)

