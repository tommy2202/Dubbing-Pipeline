# Fresh Machine Setup (Offline-Friendly)

## 1) Run doctor first

Run the setup wizard and read the report:

```
dubbing-pipeline doctor
# or
python -m dubbing_pipeline doctor
```

Reports are written to:

```
<OUTPUT_DIR>/reports/doctor_report.txt
<OUTPUT_DIR>/reports/doctor_report.json
```

The report lists **REQUIRED** vs **OPTIONAL** items, along with install steps per OS.

---

## 2) How to reach High mode

High mode expects GPU + large models. A typical checklist:

1) **GPU runtime**
   - NVIDIA drivers installed on the host
   - NVIDIA Container Toolkit installed (Linux)
   - Run container with GPU access (`--gpus all`)

2) **ASR (Whisper)**
   - Install package: `python3 -m pip install openai-whisper`
   - Download weights: `python3 -c "import whisper; whisper.load_model('large-v3')"`

3) **TTS (XTTS)**
   - Install package: `python3 -m pip install TTS`
   - Agree to Coqui TOS: `export COQUI_TOS_AGREED=1`
   - Download weights: `python3 -c "from TTS.api import TTS; TTS('tts_models/multilingual/multi-dataset/xtts_v2')"`

4) **Optional add-ons**
   - See “Optional feature packs” below.

Run doctor again to confirm readiness.

---

## 3) Optional feature packs

These are optional; the pipeline will still run without them, but with reduced quality/features.

### Diarization (speaker segmentation)
**What it does:** Splits audio by speaker and improves speaker consistency.  
**Enable:**
```
export DIARIZER=pyannote
export ENABLE_PYANNOTE=1
export HUGGINGFACE_TOKEN=...   # or HF_TOKEN
python3 -m pip install pyannote.audio
```

### Demucs (music/voice separation)
**What it does:** Improves separation of dialog vs music.  
**Enable:**
```
export SEPARATION=demucs
python3 -m pip install demucs
```

### Wav2Lip (lipsync)
**What it does:** Enhances lip synchronization for video output.  
**Enable:**
```
export LIPSYNC=wav2lip
python3 scripts/download_models.py
```

### Redis queue (scale-out)
**What it does:** Enables multi-worker queueing and better throughput.  
**Enable:**
```
export QUEUE_MODE=redis
export REDIS_URL=redis://host:6379/0
```

---

## Notes

- Doctor is **offline-friendly** and never downloads models by itself.
- Optional download steps are shown explicitly in the report.
- Secrets are **redacted** in reports (safe for CI).
## Fresh machine setup (matches CI)

These steps mirror `.github/workflows/ci.yml` so local runs match CI behavior.

### System dependencies (Ubuntu)

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends ffmpeg espeak-ng libsndfile1
```

### Python environment

```bash
python3 -m pip install --upgrade pip
python3 -m pip install -e ".[dev]"
python3 -m pip install "openai-whisper==20231117"
```

### Pre-flight guardrails

```bash
python3 scripts/check_no_tracked_artifacts.py
python3 scripts/check_no_secrets.py
python3 scripts/check_no_sensitive_runtime_files.py
```

### Package + verify release zip (offline, allowlist)

```bash
python3 scripts/package_release.py --out dist --name local-release.zip
python3 scripts/verify_release_zip.py dist/local-release.zip
```

### Test / gates (same order as CI)

```bash
make check
python3 scripts/verify_env.py
python3 scripts/polish_gate.py
python3 scripts/mobile_gate.py
python3 scripts/security_mobile_gate.py
python3 scripts/security_smoke.py
```

