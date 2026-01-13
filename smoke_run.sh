#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "${ROOT}/samples"
mkdir -p "${ROOT}/Input"

SAMPLE_MP4="${ROOT}/samples/Test.mp4"
if [[ ! -f "${SAMPLE_MP4}" ]]; then
  if command -v ffmpeg >/dev/null 2>&1; then
    # Generate a tiny synthetic sample (offline, non-sensitive).
    ffmpeg -y \
      -f lavfi -i "testsrc=size=320x180:rate=10" \
      -f lavfi -i "sine=frequency=440:sample_rate=44100" \
      -t 2.0 \
      -c:v libx264 -pix_fmt yuv420p \
      -c:a aac \
      "${SAMPLE_MP4}" >/dev/null 2>&1
  else
    echo "Missing sample MP4 and ffmpeg not found." >&2
    echo "Either install ffmpeg or provide a tiny mp4 at ${SAMPLE_MP4}" >&2
    exit 1
  fi
fi

python3 -m pip install -e "${ROOT}" >/dev/null

rm -rf "${ROOT}/Output/sample"

cp "${SAMPLE_MP4}" "${ROOT}/Input/Test.mp4"
python3 -m anime_v2.cli "${ROOT}/Input/Test.mp4" --mode low --device cpu --no-translate
rm -f "${ROOT}/Input/Test.mp4"

test -f "${ROOT}/Output/sample/sample.dub.mkv"
test -f "${ROOT}/Output/sample/sample.dub.mp4"
test -f "${ROOT}/Output/sample/audio.wav"

echo "smoke ok: Output/sample/sample.dub.mkv + .mp4"

