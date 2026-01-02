#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    cmd = [sys.executable, "-m", "pytest", "-q", str(root / "tests" / "test_mode_contract.py")]
    p = subprocess.run(cmd, check=False)
    if p.returncode != 0:
        print("FAIL: mode contract tests failed", file=sys.stderr)
        return p.returncode
    print("OK: mode contract tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

