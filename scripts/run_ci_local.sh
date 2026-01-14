#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -n "${PYTHON:-}" ]; then
  PY="${PYTHON}"
elif command -v python3.10 >/dev/null 2>&1; then
  PY="python3.10"
else
  PY="python3"
fi

echo "Running core CI locally (repo root: $ROOT)"
echo "- python: $("$PY" --version 2>&1 || true)"

# Core CI is pinned to Python 3.10 in GitHub Actions. Newer Pythons can make some
# Starlette/AnyIO TestClient shutdown behavior flaky (CancelledError).
if ! "$PY" - <<'PY'
import sys
ok = (sys.version_info.major, sys.version_info.minor) == (3, 10)
raise SystemExit(0 if ok else 1)
PY
then
  echo "ERROR: core CI expects Python 3.10." >&2
  echo "Install python3.10 or set PYTHON=/path/to/python3.10" >&2
  exit 2
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ERROR: ffmpeg not found. Install ffmpeg and retry." >&2
  exit 2
fi

if ! command -v ffprobe >/dev/null 2>&1; then
  echo "ERROR: ffprobe not found. Install ffmpeg (includes ffprobe) and retry." >&2
  exit 2
fi

echo "Installing project + dev deps..."
"$PY" -m pip install --upgrade pip
"$PY" -m pip install -e ".[dev]"
"$PY" -m pip install "openai-whisper==20231117"

echo "Guardrails..."
"$PY" scripts/check_no_tracked_artifacts.py
"$PY" scripts/check_no_secrets.py

echo "Smoke import all..."
"$PY" scripts/smoke_import_all.py

echo "Package + verify release zip..."
mkdir -p dist
"$PY" scripts/package_release.py --out dist --name local-ci-release.zip
"$PY" scripts/verify_release_zip.py dist/local-ci-release.zip

echo "Repo gates..."
"$PY" scripts/verify_env.py
"$PY" scripts/polish_gate.py
"$PY" scripts/mobile_gate.py
"$PY" scripts/security_mobile_gate.py
"$PY" scripts/security_smoke.py

echo "OK: core CI passed locally"

