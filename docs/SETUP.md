## Setup

This guide gets you from “fresh machine” to “first dubbed output”, for both CLI and Web UI.

Clean setup guide (plain text, step-by-step):
docs/CLEAN_SETUP_GUIDE.txt

If you only want to try it quickly, start with the README quickstart and come back here for production hardening.

---

## System requirements

### Minimum (CPU-only)
- **OS**: Linux is best supported (the repo also runs on macOS; Windows typically requires WSL2)
- **Python**: 3.10+
- **Disk**: plan for **10–50GB** depending on model caches and outputs
- **FFmpeg**: `ffmpeg` and `ffprobe` must be installed and on `PATH`

### Recommended (GPU)
- NVIDIA GPU with recent drivers
- Sufficient VRAM for your chosen ASR/TTS models
- For Docker GPU: NVIDIA Container Toolkit installed on the host

---

## Local install (recommended for development)

### 1) Install FFmpeg

Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg
```

Sanity check:

```bash
ffmpeg -version
ffprobe -version
```

### 2) Install the Python package

From the repo root:

```bash
python3 -m pip install -e .
```

Optional extras (pick what you need):

```bash
python3 -m pip install -e ".[translation,diarization,tts,mixing,webrtc,dev]"
```

Notes:
- Some extras (e.g. diarization / TTS) can pull large ML dependencies and may take a while.
- If you run fully offline, pre-populate caches first (see “Offline mode” below).

### 3) Configure environment files (safe defaults)

Copy templates:

```bash
cp .env.example .env
cp .env.secrets.example .env.secrets
```

Edit `.env` for non-sensitive settings, and `.env.secrets` for secrets.

#### Required secrets for production
Set strong values in `.env.secrets` (placeholders only):
- `JWT_SECRET=<base64-or-random-string>`
- `SESSION_SECRET=<base64-or-random-string>`
- `CSRF_SECRET=<base64-or-random-string>`

Optional (recommended):
- `ADMIN_USERNAME=<admin>`
- `ADMIN_PASSWORD=<strong-password>`

Generate a strong 32-byte base64 value:

```bash
python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
```

#### About the “weak_secrets_detected” warning
By default, the app warns (but continues) if it detects placeholder secrets.

To hard-fail on weak secrets (recommended for real deployments):

```bash
export STRICT_SECRETS=1
```

---

## Docker setup (recommended for self-hosted server)

### Option A: Local docker-compose (reverse proxy optional)

Use `docker/docker-compose.yml`:

```bash
docker compose -f docker/docker-compose.yml up -d --build
```

This starts the API service and (optionally) Caddy / Cloudflared profiles if you enable them.

### Option B: Production-ish compose with public HTTPS (Caddy)

Use `deploy/compose.public.yml`:

```bash
cd deploy
DOMAIN=<your-domain> CADDY_EMAIL=<you@example.com> docker compose -f compose.public.yml up -d
```

Notes:
- This assumes you have DNS pointing at the host and want Caddy-managed TLS.
- The app still enforces its own auth/CSRF/rate limiting; Caddy is just TLS + proxy.

### Option C: Cloudflare tunnel (no inbound ports)

Use `deploy/compose.tunnel.yml`:

```bash
cd deploy
CLOUDFLARE_TUNNEL_TOKEN=<token> docker compose -f compose.tunnel.yml up -d
```

Then configure the app for Cloudflare mode (see “Remote/mobile usage”).

### GPU with Docker
The default container image in `docker/Dockerfile` is CUDA-based and supports GPU when run with:

```bash
docker run --gpus all ...
```

With compose, your Docker installation must support GPU pass-through (NVIDIA Container Toolkit).

Note:
- `docker/Dockerfile.cuda` exists for legacy experimentation and does **not** represent the feature-complete stack. Prefer `docker/Dockerfile` + `docker/docker-compose.yml`.

---

## First run (CLI)

Run a test job:

```bash
ffmpeg -y \
  -f lavfi -i "testsrc=size=320x180:rate=10" \
  -f lavfi -i "sine=frequency=440:sample_rate=44100" \
  -t 2.0 \
  -c:v libx264 -pix_fmt yuv420p \
  -c:a aac \
  samples/Test.mp4

cp samples/Test.mp4 Input/Test.mp4

dubbing-pipeline Input/Test.mp4 --mode medium --device auto
```

Outputs land in:
- `Output/Test/<Test>.dub.mkv`
- `Output/Test/<Test>.dub.mp4`

Note:
- `Input/` and `Output/` are runtime folders and should not be committed (only `.gitkeep` is tracked).

If you see `weak_secrets_detected`, it’s safe for local testing; set real secrets before exposing remotely.

---

## First run (Web UI)

Start the server locally:

```bash
export HOST=0.0.0.0
export PORT=8000
dubbing-web
```

Open:
- `http://<SERVER_IP>:8000/ui/login`

Login:
- Provide `ADMIN_USERNAME`/`ADMIN_PASSWORD` in `.env.secrets` (bootstrap), or create users via admin tooling if available.

Submit a job:
- `/ui/upload`

---

## Offline mode / egress policy

The server can restrict outbound network use:
- `OFFLINE_MODE=1` (strong “no downloads” posture; requires caches already populated)
- `ALLOW_EGRESS=0` (deny outbound connections)
- `ALLOW_HF_EGRESS=1` (allow Hugging Face domains only when `ALLOW_EGRESS=0`)

Tip: if you want model downloads, keep these enabled only during cache warmup and then disable for steady-state.

---

## Remote/mobile usage (opt-in)

Remote access is **opt-in** via `REMOTE_ACCESS_MODE`:
- `REMOTE_ACCESS_MODE=off` (default)
- `REMOTE_ACCESS_MODE=tailscale` (recommended)
- `REMOTE_ACCESS_MODE=cloudflare` (optional, requires proxy-safe settings)

Start modes:

```bash
# Tailscale (recommended)
export REMOTE_ACCESS_MODE=tailscale
export HOST=0.0.0.0
export PORT=8000
dubbing-web

# Cloudflare (only if you’re behind Cloudflare Tunnel/Access)
export REMOTE_ACCESS_MODE=cloudflare
export TRUST_PROXY_HEADERS=1
export HOST=0.0.0.0
export PORT=8000
dubbing-web
```

Details:
- `docs/remote_access.md`
- `docs/mobile_remote.md`
- `docs/WEB_MOBILE.md`

Security recommendations for remote:
- set `COOKIE_SECURE=1` when traffic is HTTPS (tunnel / Caddy)
- set strict `CORS_ORIGINS` to your actual UI origin(s)
- use `STRICT_SECRETS=1`
- keep legacy token login disabled (`ALLOW_LEGACY_TOKEN_LOGIN=0`)

---

## Privacy mode + encryption at rest (optional)

### Privacy mode (per-job)
Privacy controls are **off by default**.

CLI:
- `--privacy on`
- `--no-store-transcript`
- `--no-store-source-audio`
- `--minimal-artifacts`

Web:
- privacy-related toggles are applied per job based on server defaults and job runtime settings.

### Encryption at rest (server + web uploads)
Encryption is **off by default**.

To enable:
- `.env`:
  - `ENCRYPT_AT_REST=1`
  - optionally `ENCRYPT_AT_REST_CLASSES=uploads,review,transcripts,voice_memory`
- `.env.secrets`:
  - `ARTIFACTS_KEY=<base64-32-bytes>` (required when encryption is enabled)

Important:
- If encryption is enabled but the key is missing/invalid, the server **fails safe** (it won’t silently write plaintext).

---

## Retention (optional)

Per-job retention controls (CLI and Web):
- `--cache-policy full|balanced|minimal` (default `full`)
- `--retention-days N` age gate (default `0`, i.e., no age gating)
- `--retention-dry-run` to produce a report without deleting

Report:
- `Output/<stem>/analysis/retention_report.json`

Global best-effort cleanup (old uploads/logs):
- configured by `RETENTION_DAYS_INPUT` and `RETENTION_DAYS_LOGS`
- run manually:

```bash
python3 -m dubbing_pipeline.ops.retention
```

