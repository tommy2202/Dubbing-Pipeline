# Setup / Health (Guided Setup)

The **Setup / Health** page (`/ui/setup`) provides an actionable checklist powered by the doctor subsystem.
It is designed to be safe for shared environments: **no secrets, tokens, or cookies are ever printed.**

## How to use

1. Log into the web UI.
2. Open **Setup / Health** from the header.
3. Follow the remediation commands or links for any item marked **MISSING** or **BROKEN**.

## Core required checks

### Core: ffmpeg

**Why:** Enables audio/video extraction, muxing, and previews.  
**Fix (Linux):**

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg
```

### Core: ASR low-mode

**Why:** Enables transcription in low mode (ASR).  
**Fix:**

```bash
python3 -m pip install openai-whisper
python3 -c "import whisper; whisper.load_model('medium')"
```

### Core: basic TTS

**Why:** Enables basic text-to-speech output.  
**Fix:**

```bash
export TTS_PROVIDER=auto
python3 -m pip install TTS
```

### Core: storage dirs writable

**Why:** Uploads and outputs must be written to disk.  
**Fix:** Ensure these paths are writable for the service user:

```
APP_ROOT/Input
APP_ROOT/Output
Output/_state
```

### Core: auth/CSRF configured

**Why:** Enables secure sessions, CSRF protection, and API auth.  
**Fix:**

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
export JWT_SECRET="..."
export CSRF_SECRET="..."
export SESSION_SECRET="..."
export API_TOKEN="..."
export ADMIN_USERNAME="admin"
export ADMIN_PASSWORD="..."
```

### Core: remote access mode ok

**Why:** Ensures access posture matches your chosen mode (off/tailscale/cloudflare).  
**Fix:** Review `ACCESS_MODE` / `REMOTE_ACCESS_MODE` and proxy trust settings.

## Optional checks

### Optional: GPU acceleration

**Why:** Faster transcription and TTS with CUDA.  
**Fix:** Install NVIDIA drivers + CUDA and a compatible PyTorch wheel.

### Optional: Whisper large-v3

**Why:** Higher-accuracy ASR model for best transcription quality.  
**Fix:**

```bash
python3 -m pip install openai-whisper
python3 -c "import whisper; whisper.load_model('large-v3')"
```

### Optional: XTTS voice cloning

**Why:** Higher-fidelity voice cloning.  
**Fix:**

```bash
export TTS_PROVIDER=auto
export COQUI_TOS_AGREED=1
python3 -m pip install TTS
python3 -c "from TTS.api import TTS; TTS('tts_models/multilingual/multi-dataset/xtts_v2')"
```

### Optional: diarization

**Why:** Speaker separation for multi-speaker content.  
**Fix (speechbrain):**

```bash
export DIARIZER=speechbrain
python3 -m pip install speechbrain
```

### Optional: vocals separation

**Why:** Music/voice separation for cleaner mixes.  
**Fix:**

```bash
export SEPARATION=demucs
python3 -m pip install demucs
```

### Optional: lipsync plugin (Wav2Lip)

**Why:** Lip-sync enhancement for video outputs.  
**Fix:**

```bash
export LIPSYNC=wav2lip
python3 scripts/download_models.py
```

### Optional: Redis queue

**Why:** Shared queue backend for multi-worker deployments.  
**Fix:**

```bash
export REDIS_URL="redis://host:6379/0"
```

### Optional: TURN/WebRTC relay

**Why:** WebRTC relay when direct peer connections fail.  
**Fix:**

```bash
export TURN_URL="turns:turn.example.com:3478"
export TURN_USERNAME="user"
export TURN_PASSWORD="password"
```

## Notes

- The setup page is safe to refresh; it does not mutate state.
- If a check is **BROKEN**, fix configuration first before re-running workflows.
## Setup Wizard / Doctor

The **doctor** checks the environment and produces a redacted report that you
can share for support. It comes in two parts:

- **Host doctor**: runs on the host to validate Docker, GPU, and host services.
- **Container doctor**: runs inside the container to validate pipeline wiring and (in full mode) run a tiny end-to-end job.

### Host doctor (on the host)

```bash
python3 scripts/doctor_host.py
python3 scripts/doctor_host.py --require-gpu
```

### Container doctor (inside the container)

```bash
dubbing-pipeline doctor --mode quick
dubbing-pipeline doctor --mode full
dubbing-pipeline doctor --mode quick --json
```

---

## How to interpret PASS / WARN / FAIL

- **PASS**: check is healthy.
- **WARN**: optional or degraded behavior (pipeline still works but feature may be missing).
- **FAIL**: required for the selected mode; fix before running production workloads.

The report includes remediation commands (copy/paste). Reports are **always
redacted**.

---

## Common fixes

### CUDA mismatch / missing GPU access
- Confirm NVIDIA drivers on host (`nvidia-smi`)
- Container runtime needs GPU access:
  - `docker run --gpus all ...`
  - Install NVIDIA Container Toolkit on host

### Toolkit missing
- Install toolkit (Ubuntu example):
  - `sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit`
  - `sudo systemctl restart docker`

### Model weights missing
- Use the included download helper:
  - `python3 scripts/download_models.py`
- Or load once to populate caches:
  - `python3 -c "import whisper; whisper.load_model('medium')"`
  - `python3 -c "from TTS.api import TTS; TTS('tts_models/multilingual/multi-dataset/xtts_v2')"`

### Ports in use
- Check port usage:
  - `lsof -iTCP -sTCP:LISTEN -n -P`
- Set a different port:
  - `export PORT=8000`

### Permissions / writable dirs
- Ensure Input/Output/Logs are writable:
  - `mkdir -p Input Output Logs`
  - `chmod -R u+rwX Input Output Logs`

---

## Security note

- The doctor **never prints secret values**.
- Reports explicitly note **"secrets redacted"**.
- If you need to share a report, it is safe to paste as-is.
