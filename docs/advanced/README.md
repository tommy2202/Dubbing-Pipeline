# Advanced deployment docs (not the Golden Path)

The **recommended** and safest remote-access method is **Tailscale**:
- See `docs/GOLDEN_PATH_TAILSCALE.md`

Everything below is “advanced”: useful in specific scenarios, but easier to misconfigure.

## Remote access (deep dive)
- Tailscale + Cloudflare Tunnel + Access: `docs/remote_access.md`
- Phone-first summary: `docs/mobile_remote.md`

## Public HTTPS / production compose
- Public HTTPS + Caddy compose: `deploy/compose.public.yml` (advanced)
- Tunnel compose: `deploy/compose.tunnel.yml` (advanced)
- Deployment notes: `README-deploy.md`

