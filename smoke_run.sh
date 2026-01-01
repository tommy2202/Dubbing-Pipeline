#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "${ROOT}/samples"

SAMPLE_MP4="${ROOT}/samples/sample.mp4"
if [[ ! -f "${SAMPLE_MP4}" ]]; then
  if [[ -f "${ROOT}/Input/Test.mp4" ]]; then
    cp "${ROOT}/Input/Test.mp4" "${SAMPLE_MP4}"
  else
    echo "Missing sample MP4. Put a 5–10s mp4 at samples/sample.mp4" >&2
    exit 1
  fi
fi

python3 -m pip install -e "${ROOT}" >/dev/null

rm -rf "${ROOT}/Output/sample"

python3 -m anime_v2.cli "${SAMPLE_MP4}" --mode low --device cpu --no-translate

test -f "${ROOT}/Output/sample/sample.dub.mkv"
test -f "${ROOT}/Output/sample/sample.dub.mp4"
test -f "${ROOT}/Output/sample/audio.wav"

echo "smoke ok: Output/sample/sample.dub.mkv + .mp4"

