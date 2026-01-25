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
