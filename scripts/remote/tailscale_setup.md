## Tailscale setup (server + phone) for anime-v2-web

Goal: access the web UI from your phone on mobile data without port forwarding.

### Server laptop
1) Install Tailscale:
- Linux: follow Tailscale’s official install instructions for your distro.
- macOS/Windows: install the desktop app.

2) Log in to your tailnet and connect.

3) Start the server with allowlist enforcement:

```bash
export REMOTE_ACCESS_MODE=tailscale
export HOST=0.0.0.0
export PORT=8000
anime-v2-web
```

4) Print the URL to open on phone:

```bash
python3 scripts/remote/tailscale_check.py
```

### Phone
1) Install Tailscale from the app store.
2) Log in to the same tailnet.
3) Open the URL printed by `tailscale_check.py`:
   - usually `http://<tailscale-ip>:8000/ui/login`

### Troubleshooting
- If you get **403 Forbidden**:
  - confirm `REMOTE_ACCESS_MODE=tailscale`
  - confirm you’re hitting the **Tailscale IP**, not the LAN IP
  - if you use IPv6 or custom routes, set `ALLOWED_SUBNETS` explicitly (see `docs/remote_access.md`).

