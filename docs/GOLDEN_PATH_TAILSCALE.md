# Golden Path (Tailscale) — safe remote access, no public exposure

This is the recommended way to use the **Dubbing Pipeline web UI from your phone on mobile data** without opening your laptop/server to the public internet.

**Security model (recommended):**
- You do **not** expose the service publicly.
- You **invite specific people** into your Tailscale network (tailnet).
- Access is only possible for devices that are:
  - logged into your tailnet, and
  - allowed by the app’s built-in “Tailscale mode” IP allowlist, and
  - authenticated in the web UI (username/password).

---

## What you will do

- Install Tailscale on the **server** (the machine running the app) and on the **client** (your phone).
- Start the web server in **Tailscale mode** (safe allowlist on).
- Open the web UI using the server’s **tailnet IP** (not the LAN IP).
- Run your first job end-to-end from the phone.

---

## 1) Install Tailscale (server + phone)

### Server machine
1. Install Tailscale from the official instructions for your OS.
2. Log in to your tailnet.
3. Confirm it’s running:

```bash
tailscale status
```

### Phone
1. Install the Tailscale app.
2. Log in to the **same tailnet** as the server.

---

## 2) Recommended sharing model (no public exposure)

The safest model is:
- You own the tailnet.
- You invite only the people/devices you trust (family/team).
- You **do not** publish the service on the public internet.

Recommended Tailscale settings:
- **Disable** “public share links” / “funnel” for this service.
- Prefer inviting users directly to your tailnet, or using device approvals if you want tighter control.

---

## 3) Start the server (safe defaults)

### Option A (recommended): use the run script (Linux/macOS)

From the repo root:

```bash
./scripts/run_prod.sh
```

What it does:
- validates prerequisites (python + ffmpeg/ffprobe; docker optional)
- ensures `.env` and `.env.secrets` exist and are safe
- starts the web server in **Tailscale mode**
- prints the **local URL**, the **Tailscale URL**, and where logs are

### Option B: use the run script (Windows PowerShell)

From the repo root:

```powershell
.\scripts\run_prod.ps1
```

---

## 4) Find the server tailnet IP (the URL you open on your phone)

Run:

```bash
python3 scripts/remote/tailscale_check.py
```

It prints a URL like:
- `http://100.x.y.z:8000/ui/login`

Important:
- Use the **Tailscale IP** (`100.64.0.0/10` range), not your LAN IP (like `192.168.x.x`).

---

## 5) Access the web UI from your phone

1. Make sure Tailscale is **connected** on the phone.
2. Open the URL printed by the check script:
   - `http://<tailscale-ip>:8000/ui/login`
3. Log in.

If you get **403 Forbidden**:
- you’re probably using the wrong IP (LAN instead of tailnet), or
- the server is not running in Tailscale mode.

---

## 6) First job workflow (end-to-end)

1. In the web UI, go to **Upload Wizard**.
2. Upload a small test video (MP4 recommended).
3. Submit the job (defaults are safe).
4. Watch progress on the job page.
5. When it finishes:
   - use the **mobile playback** output (MP4) on the job page
   - optionally download final artifacts from the job “files” view

Notes:
- The server writes outputs under `Output/` and logs under `logs/` (the scripts print exact paths).
- You can stop the server at any time; unfinished jobs are recovered on restart.

---

## Troubleshooting (quick)

### Phone can’t connect
- Confirm both devices are logged into the **same tailnet**.
- Confirm server is listening on `0.0.0.0:8000`.
- Re-run:

```bash
python3 scripts/remote/tailscale_check.py
```

### 403 Forbidden
- Confirm you opened the **Tailscale IP**, not the LAN IP.
- Confirm you started the server with `REMOTE_ACCESS_MODE=tailscale` (the scripts do this).

### Want HTTPS / Cloudflare / public deployment?
Those are intentionally **advanced** and are documented separately (see `docs/advanced/`).

