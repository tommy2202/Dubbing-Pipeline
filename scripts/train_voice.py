#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    """
    Fine-tuning hook placeholder (F9).

    This repo is offline-first and does not ship heavyweight training deps by default.
    This script defines a clear contract for future voice fine-tuning integrations.

    Expected inputs:
      - A folder of audio clips (wav) + transcripts (txt/json) per speaker.
      - A base TTS model identifier (e.g. Coqui XTTS v2) or checkpoint path.

    Expected outputs:
      - A model artifact directory that can be referenced by TTS provider selection,
        or a speaker embedding/ref-wav store that improves cloning quality.
    """
    ap = argparse.ArgumentParser(description="Voice fine-tuning hook (optional).")
    ap.add_argument("--data", type=Path, required=True, help="Training data directory")
    ap.add_argument("--out", type=Path, required=True, help="Output directory for artifacts")
    ap.add_argument("--base-model", default="xtts_v2", help="Base model identifier")
    args = ap.parse_args()

    if not args.data.exists():
        print(f"Data path not found: {args.data}", file=sys.stderr)
        return 2

    # We do NOT attempt to train by default to avoid giving a false impression.
    print(
        "Training is not implemented in this repository by default.\n\n"
        "To add training:\n"
        "- Implement a trainer that consumes --data and writes artifacts to --out\n"
        "- Add a TTS provider that can load those artifacts\n"
        "- Keep it optional behind extra dependencies\n",
        file=sys.stderr,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "README.txt").write_text(
        "Placeholder output directory for future voice fine-tuning artifacts.\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
