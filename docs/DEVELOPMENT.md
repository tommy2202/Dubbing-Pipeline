# Developer Quickstart

## Install (editable + dev extras)

```bash
python3 -m pip install -e ".[dev]"
```

## Run unit tests

```bash
pytest -q
```

Notes:
- Some tests are skipped when optional dependencies (e.g., whisper) are missing.
- If you want startup checks (ffmpeg/ffprobe) enabled during tests, set:
  - `DUBBING_SKIP_STARTUP_CHECK=0`

## Run v0 gate

```bash
python3 scripts/polish_gate.py
```
