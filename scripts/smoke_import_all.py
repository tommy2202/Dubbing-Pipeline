#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from importlib import import_module
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    # Ensure we don't enforce secrets during import smoke.
    os.environ.setdefault("STRICT_SECRETS", "0")

    # Keep imports lightweight / deterministic.
    os.environ.setdefault("OFFLINE_MODE", "1")
    os.environ.setdefault("ALLOW_EGRESS", "0")
    os.environ.setdefault("ALLOW_HF_EGRESS", "0")
    os.environ.setdefault("ENABLE_PYANNOTE", "0")
    os.environ.setdefault("COQUI_TOS_AGREED", "0")

    try:
        for mod in (
            "config.settings",
            "anime_v2.config",
            "anime_v2.audio.separation",
            "anime_v2.audio.mix",
            "anime_v2.timing.fit_text",
            "anime_v2.timing.pacing",
            "anime_v2.voice_memory.store",
            "anime_v2.voice_memory.embeddings",
            "anime_v2.review.state",
            "anime_v2.review.ops",
            "anime_v2.review.cli",
            "anime_v2.jobs.queue",
            "anime_v2.realtime",
            "anime_v2.server",
            "anime_v2.web.app",
            "anime_v2.cli",
            "main",
        ):
            import_module(mod)
    except Exception as ex:
        print(f"IMPORT_SMOKE_FAILED: {ex}", file=sys.stderr)
        return 2

    print("IMPORT_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
