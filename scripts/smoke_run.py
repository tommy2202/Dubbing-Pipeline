#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    """
    Minimal smoke run.

    - Always: print safe config report + import smoke
    - Optional: run a low-mode CPU pass if `samples/sample.mp4` exists AND
      the user sets SMOKE_RUN_PIPELINE=1 (prevents accidental heavy runs).
    """
    repo = Path(__file__).resolve().parents[1]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))

    os.environ.setdefault("STRICT_SECRETS", "0")
    os.environ.setdefault("OFFLINE_MODE", "1")
    os.environ.setdefault("ALLOW_EGRESS", "0")
    os.environ.setdefault("ALLOW_HF_EGRESS", "0")
    os.environ.setdefault("ENABLE_PYANNOTE", "0")
    os.environ.setdefault("COQUI_TOS_AGREED", "0")

    from config.settings import get_safe_config_report

    print("SAFE_CONFIG_REPORT:")
    import json as _json

    print(_json.dumps(get_safe_config_report(), indent=2, sort_keys=True))

    # Import smoke
    from importlib import import_module

    for mod in ("anime_v2.server", "anime_v2.cli", "anime_v2.web.app"):
        import_module(mod)
    print("IMPORT_SMOKE_OK")

    if os.environ.get("SMOKE_RUN_PIPELINE", "0") != "1":
        print("Skipping pipeline run (set SMOKE_RUN_PIPELINE=1 to enable).")
        return 0

    sample = repo / "samples" / "sample.mp4"
    if not sample.exists():
        print(f"Sample missing: {sample}", file=sys.stderr)
        return 2

    from anime_v2.cli import cli

    # Low-mode, CPU, no-translate, no mux subs, minimal output
    cli.main(
        args=[
            str(sample),
            "--mode",
            "low",
            "--device",
            "cpu",
            "--no-translate",
            "--no-subs",
            "--subs",
            "both",
            "--subs-format",
            "srt",
        ],
        standalone_mode=False,
    )
    print("PIPELINE_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
