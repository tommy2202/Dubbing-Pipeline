#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    """
    Minimal smoke run.

    - Always: print safe config report + import smoke
    - Optional: run a low-mode CPU pass if `samples/Test.mp4` exists (or can be generated) AND
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

    for mod in ("dubbing_pipeline.server", "dubbing_pipeline.cli", "dubbing_pipeline.web.app"):
        import_module(mod)
    print("IMPORT_SMOKE_OK")

    if os.environ.get("SMOKE_RUN_PIPELINE", "0") != "1":
        print("Skipping pipeline run (set SMOKE_RUN_PIPELINE=1 to enable).")
        return 0

    sample = repo / "samples" / "Test.mp4"
    if not sample.exists():
        # Prefer generating a tiny synthetic sample (offline) over shipping real media.
        if not shutil.which("ffmpeg"):
            print(f"Sample missing and ffmpeg not found: {sample}", file=sys.stderr)
            return 2
        sample.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x180:rate=10",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=44100",
                "-t",
                "2.0",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(sample),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    from dubbing_pipeline.cli import cli

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
