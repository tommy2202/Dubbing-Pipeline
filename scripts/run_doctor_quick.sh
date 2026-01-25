#!/usr/bin/env bash
set -euo pipefail

# Minimal quick doctor run with safe defaults.
# Avoids hard failures on missing optional models by forcing low mode.
export DUBBING_DOCTOR_MODE="${DUBBING_DOCTOR_MODE:-low}"
export ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-adminpass}"

python3 -m dubbing_pipeline.cli doctor --mode quick --json
