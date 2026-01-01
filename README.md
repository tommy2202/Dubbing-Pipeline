## Anime dubbing pipeline (v2)

This repo contains an **offline dubbing pipeline** (CLI) and a **lightweight web player** for watching completed episodes from a phone on your LAN.

- **CLI**: `anime-v2`
- **Web player**: `anime-v2-web` (FastAPI + Range streaming)
- **Outputs**: `Output/<video_stem>/...` (intermediate artifacts + final `dub.mkv`)

---

## Quickstart (local)

### Prereqs

- Python **3.10+**
- `ffmpeg` available on PATH

### Install

```bash
python3 -m pip install -e .
```

Optional extras (recommended on a real machine with enough disk/RAM):

```bash
python3 -m pip install -e ".[translation,diarization,tts,dev]"
```

### Run a dub job

```bash
anime-v2 Input/Test.mp4 --mode medium --device auto
```

Artifacts will land in:

- `Output/Test/audio.wav`
- `Output/Test/diarization.json`
- `Output/Test/Test.srt` (ASR)
- `Output/Test/translated.json` + `Output/Test/Test.translated.srt` (when translation enabled)
- `Output/Test/Test.tts.wav`
- `Output/Test/dub.mkv`

---

## Quickstart (Docker, GPU)

### Build

```bash
docker build -f docker/Dockerfile -t anime-v2-gpu .
```

### Run (CLI)

```bash
docker run --rm --gpus all \
  -v "$(pwd)":/app \
  -v "$(pwd)/Input":/app/Input \
  -v "$(pwd)/Output":/app/Output \
  anime-v2-gpu /app/Input/Test.mp4 --mode high --device cuda
```

### Run (web player)

```bash
docker run --rm --gpus all -p 8000:8000 \
  -e API_TOKEN=change-me \
  -v "$(pwd)/Output":/app/Output \
  anime-v2-gpu anime-v2-web
```

---

## Windows examples

### CMD

**Build**

```bat
docker build -f docker\Dockerfile -t anime-v2-gpu .
```

**Run (CLI, GPU)**

```bat
docker run --rm --gpus all ^
  -v "%cd%":/app ^
  -v "%cd%\Input":/app/Input ^
  -v "%cd%\Output":/app/Output ^
  anime-v2-gpu /app/Input/Test.mp4 --mode high --device cuda
```

**Run (web player, GPU)**

```bat
docker run --rm --gpus all -p 8000:8000 ^
  -e API_TOKEN=change-me ^
  -v "%cd%\Output":/app/Output ^
  anime-v2-gpu anime-v2-web
```

### PowerShell

```powershell
docker run --rm --gpus all `
  -v "${PWD}:/app" `
  -v "${PWD}/Input:/app/Input" `
  -v "${PWD}/Output:/app/Output" `
  anime-v2-gpu /app/Input/Test.mp4 --mode high --device cuda
```

---

## Modes

The `--mode` flag selects a Whisper ASR model:

| Mode | Whisper model | Expected quality | Expected runtime |
|------|---------------|------------------|------------------|
| high | `large-v3`    | best             | slowest          |
| medium | `medium`    | good default     | medium           |
| low  | `small`       | acceptable       | fastest          |

Device selection:

- `--device auto`: uses CUDA if available, else CPU
- `--device cuda` / `--device cpu`: force

---

## `.env` setup + API token (web UI)

Copy and edit:

```bash
cp .env.example .env
```

Set at least:

- `API_TOKEN` (use a long random value)

Start the web player:

```bash
anime-v2-web
```

Login from your phone browser:

- `http://<your-laptop-ip>:8000/login?token=<API_TOKEN>`

After login, the token is stored in an **HTTP-only cookie** and the player page lists available files and streams them with **Range requests** (seeking works).

---

## Voice cloning + presets (fallback behavior)

### Cloning (best quality when available)

If `TTS_SPEAKER_WAV` is set (or if diarization produced a representative wav for a speaker), the TTS stage will attempt **zero-shot cloning**.

If cloning fails, it logs:

- `clone failed; using preset <name>`

### Presets (when cloning isn’t available)

Place sample WAVs here:

- `voices/presets/alice/*.wav`
- `voices/presets/bob/*.wav`

Build embeddings:

```bash
python tools/build_voice_db.py
```

This creates:

- `voices/presets.json`
- `voices/embeddings/<preset>.npy`

When speaker embeddings exist, the pipeline chooses the closest preset via cosine similarity. If no match info is available, it falls back to `TTS_SPEAKER` (default `"default"`).

---

## Security notes

- **Don’t expose this service publicly without HTTPS.**
- If you need internet access, use a tunnel that provides HTTPS (e.g. ngrok / Cloudflare Tunnel) and set a strong `API_TOKEN`.
- The server **does not log tokens** (request logs omit query strings), and auth failures are rate-limited per IP.
- Logs are written to `logs/app.log` with rotation.

---

## WebRTC Preview

If a mobile browser struggles with MKV seeking/compatibility over HTTP range streaming, you can use an optional **server-push WebRTC preview** that streams the **finished output file**.

- **Endpoint**: `POST /webrtc/offer` (used by the demo page)
- **Demo page**: `/webrtc/demo?token=<API_TOKEN>`

### STUN vs TURN (important)

- **LAN use**: STUN alone is typically fine.
- **Public internet / NAT traversal**: you usually need a **TURN server**.

Env configuration:

- `WEBRTC_STUN` (default `stun:stun.l.google.com:19302`)
- `TURN_URL` (optional, e.g. `turn:turn.example.com:3478`)
- `TURN_USERNAME` (optional)
- `TURN_PASSWORD` (optional)

Notes:

- This is a **preview of the completed MKV/MP4** (not a live encode of a running job).
- No mic/camera permissions are requested (server-push only).
- If your browser plays the MKV fine with the normal player, prefer the simpler HTTP range streaming path.

---

## Troubleshooting

### CUDA / GPU not detected

- Docker: ensure you run with `--gpus all` and have NVIDIA drivers + container toolkit installed.
- Local: try `--device cpu` to confirm the pipeline works, then revisit CUDA setup.

### Model downloads are slow / fail

- First run downloads large models (Whisper + XTTS). Ensure disk space and a stable connection.
- Use `HF_HOME`, `TORCH_HOME`, and `TTS_HOME` to relocate caches.

### Coqui TTS Terms of Service (required)

The Coqui XTTS engine requires explicit acknowledgement:

- `COQUI_TOS_AGREED=1`

Without it, the TTS layer refuses to synthesize.

### Docker `numpy==1.22.0` pin rationale

The Docker constraints pin `numpy==1.22.0` to satisfy **wheel compatibility for `TTS==0.22.0` on Python 3.10** in a reproducible way.

### No subtitles in output

- If ASR produces an empty SRT (e.g. Whisper not installed/downloaded), muxing skips subtitles to avoid ffmpeg errors on empty SRT.
- If you want to skip subs intentionally, pass `--no-subs`.

---

## Backups & retention (ops)

- **Retention**: `RETENTION_DAYS_INPUT` (default `7`) best-effort purges old raw uploads from `Input/uploads/`. `RETENTION_DAYS_LOGS` (default `14`) purges old files from `logs/` and old per-job `Output/**/job.log`.
  - Run manually: `python -m anime_v2.ops.retention`

- **Backups**: creates a metadata-only zip (no large media) under `backups/`:
  - Includes: `data/**`, `Output/*.db`, `Output/**/{*.json,*.srt,job.log}`
  - Run manually: `python -m anime_v2.ops.backup`
  - Output: `backups/backup-YYYYmmdd-HHMM.zip` and `backups/backup-YYYYmmdd-HHMM.manifest.json` (with per-file SHA256).
  - **Restore**: unzip into the repo/app root (so paths like `data/...` and `Output/...` land in place), then verify file hashes against the manifest.