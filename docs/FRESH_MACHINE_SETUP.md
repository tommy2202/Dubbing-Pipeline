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

