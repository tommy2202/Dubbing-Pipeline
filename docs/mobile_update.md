## Mobile Update (remote-friendly, production-hardened)

This repo includes a **mobile-safe FastAPI web UI** for:
- logging in securely (no token-in-URL required)
- submitting jobs from a phone (chunked upload or server file picker)
- monitoring progress/logs
- playback via **mobile-friendly MP4 (H.264/AAC)** and optional HLS
- QA + review/edit loop for iterative improvement

The canonical server entrypoint is:
- `src/anime_v2/server.py` (FastAPI `app`)

---

### LAN usage (same Wi‑Fi)

1) Start the server:

```bash
export REMOTE_ACCESS_MODE=off
export HOST=0.0.0.0
export PORT=8000
anime-v2-web
```

2) From your phone (on the same Wi‑Fi), open:
- `http://<server-lan-ip>:8000/ui/login`

3) Login with username/password and submit a job.

Notes:
- If you enable `ALLOW_LEGACY_TOKEN_LOGIN=1`, it is **LAN-only** and **unsafe on public networks**.

---

### Remote usage (mobile data) — recommended: Tailscale

Tailscale gives you private remote access without port-forwarding.

1) Install Tailscale on:
- the server laptop
- your phone

2) Start the server in Tailscale mode:

```bash
export REMOTE_ACCESS_MODE=tailscale
export HOST=0.0.0.0
export PORT=8000
anime-v2-web
```

3) Run the helper to find the exact URL:

```bash
python3 scripts/remote/tailscale_check.py
```

Open the printed URL on your phone (typically `http://<tailscale-ip>:8000/ui/login`).

---

### Remote usage (optional): Cloudflare Tunnel + Access

Use this if you want an HTTPS URL without installing Tailscale on the phone.

1) Start the app in Cloudflare mode:

```bash
export REMOTE_ACCESS_MODE=cloudflare
export TRUST_PROXY_HEADERS=1
export HOST=0.0.0.0
export PORT=8000
anime-v2-web
```

2) Configure the tunnel + Access:
- `docs/remote_access.md`
- `scripts/remote/cloudflared/README.md`

Important:
- Proxy headers are only trusted when `REMOTE_ACCESS_MODE=cloudflare`.
- Do not commit tunnel tokens or Access secrets.

---

## Phone workflow (end-to-end)

1) Open `/ui/login` and sign in.
2) Create a job via `/ui/upload`:
   - choose **Mode**, optional **Project profile**, and **PG** toggle
   - upload a file (chunked upload) OR select a server-local file
3) Open the job:
   - **Playback**: use **Mobile MP4** (or “Open in VLC”)
   - **Progress/Logs**: monitor progress + log tail
   - **QA**: tap **Fix** to jump into Review/Edit
   - **Review/Edit**: edit text, regen, preview, lock/unlock
   - **Overrides**: music regions + speaker overrides

---

## Verification (synthetic, no real anime required)

Run the single end-to-end gate:

```bash
python3 scripts/mobile_gate.py
```

It verifies:
- remote access enforcement logic (off / tailscale / cloudflare)
- auth flow (no legacy token)
- chunked upload → job creation → queue completion
- mobile MP4 exists and supports Range playback
- QA artifacts exist
- review endpoints (edit/helper/regen/audio/lock/unlock)
- logs endpoint

---

## Troubleshooting

- **403 Forbidden when remote**:
  - Tailscale mode: confirm you’re using the **Tailscale IP**
  - Cloudflare mode: ensure `TRUST_PROXY_HEADERS=1` and traffic is coming from the trusted local proxy
- **No audio regen**:
  - regen falls back to silence when TTS engines are unavailable; this is expected on minimal installs
- **Playback issues on iOS**:
  - prefer `mobile.mp4` and/or “Open in VLC”

