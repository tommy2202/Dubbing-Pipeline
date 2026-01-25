#!/usr/bin/env python3
from __future__ import annotations

import tempfile
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tests._helpers.smoke_fresh_machine import run_smoke_fresh_machine


def main() -> int:
    try:
        with tempfile.TemporaryDirectory() as td:
            run_smoke_fresh_machine(Path(td), ffmpeg_skip_message=None)
    except RuntimeError as ex:
        msg = str(ex)
        if "ffmpeg" in msg.lower():
            print("ffmpeg not available; install ffmpeg to run smoke test")
            return 2
        print(f"smoke_fresh_machine failed: {msg}")
        return 2
    print("smoke_fresh_machine: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
