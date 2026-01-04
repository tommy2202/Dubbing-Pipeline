from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    """
    Worker for batch mode.

    The parent process writes a JSON spec file:
      {"args": ["Input/Test.mp4", "--mode", "medium", ...]}

    This worker runs the Click command in a fresh process to avoid
    shared global state across heavy ML deps.
    """
    if len(sys.argv) != 2:
        print("Usage: python -m anime_v2.batch_worker <spec.json>", file=sys.stderr)
        return 2

    spec_path = Path(sys.argv[1]).expanduser()
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except Exception as ex:
        print(f"Failed to read spec {spec_path}: {ex}", file=sys.stderr)
        return 2

    args = spec.get("args")
    if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
        print("Invalid spec: expected {'args': [<str>...]}", file=sys.stderr)
        return 2

    try:
        from anime_v2.cli import cli

        cli.main(args=args, standalone_mode=False)
        return 0
    except Exception as ex:
        print(f"Worker failed: {ex}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
