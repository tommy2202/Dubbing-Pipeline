#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-}"
if [[ -z "${PY}" ]]; then
  if command -v python3 >/dev/null 2>&1; then PY="python3"; else PY="python"; fi
fi

"$PY" -m pip install -r requirements.txt -c constraints.txt
"$PY" -m pip install -r requirements-dev.txt -c constraints.txt
