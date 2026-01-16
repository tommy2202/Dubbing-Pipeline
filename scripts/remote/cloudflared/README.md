## Cloudflare Tunnel + Access (browser URL, optional)

This is the “no app on phone” option: you access the web UI via a normal HTTPS URL.

### What you need
- A Cloudflare account + a domain
- `cloudflared` (locally or via docker)
- Cloudflare Zero Trust enabled for **Access**

### Tunnel setup (high-level)
1) Install cloudflared and authenticate:

```bash
cloudflared tunnel login
```

2) Create a tunnel:

```bash
cloudflared tunnel create dubbing-pipeline
```

3) Create a DNS route to your tunnel:

```bash
cloudflared tunnel route dns dubbing-pipeline <YOUR_HOSTNAME>
```

4) Run cloudflared:
- Docker compose option already exists: `deploy/compose.tunnel.yml`
- Or use a config file like `scripts/remote/cloudflared/config.yml` (template).

### Protect with Cloudflare Access (recommended)
In Cloudflare Zero Trust:
- Add an Access application for `<YOUR_HOSTNAME>`
- Require:
  - One-time PIN OR
  - OIDC/SAML (Google/GitHub/etc)

### Origin enforcement (recommended)
To make the origin also verify Access, set in the app environment:

```bash
REMOTE_ACCESS_MODE=cloudflare
TRUST_PROXY_HEADERS=1
CLOUDFLARE_ACCESS_TEAM_DOMAIN="your-team"
CLOUDFLARE_ACCESS_AUD="your-access-app-aud"
```

This makes the server require and verify:
- `Cf-Access-Jwt-Assertion`

Notes:
- JWKS verification fetch may require `ALLOW_EGRESS=1` (default).
- No secrets are stored in git; put tokens in `.env.secrets`.

