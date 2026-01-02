#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import traceback
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

    modules = (
            "config.settings",
            "anime_v2.config",
            "anime_v2.utils.ffmpeg_safe",
            "anime_v2.utils.log",
            "anime_v2.audio.separation",
            "anime_v2.audio.mix",
            "anime_v2.audio.tracks",
            "anime_v2.audio.music_detect",
            "anime_v2.text.pg_filter",
            "anime_v2.text.style_guide",
            "anime_v2.diarization.smoothing",
            "anime_v2.timing.fit_text",
            "anime_v2.timing.pacing",
            "anime_v2.voice_memory.store",
            "anime_v2.voice_memory.embeddings",
            "anime_v2.review.state",
            "anime_v2.review.ops",
            "anime_v2.review.cli",
            "anime_v2.qa.scoring",
            "anime_v2.qa.cli",
            "anime_v2.plugins.lipsync.base",
            "anime_v2.plugins.lipsync.registry",
            "anime_v2.plugins.lipsync.wav2lip_plugin",
            "anime_v2.expressive.prosody",
            "anime_v2.expressive.policy",
            "anime_v2.expressive.director",
            "anime_v2.streaming.chunker",
            "anime_v2.streaming.runner",
            "anime_v2.stages.audio_extractor",
            "anime_v2.stages.transcription",
            "anime_v2.stages.translation",
            "anime_v2.stages.diarization",
            "anime_v2.stages.tts",
            "anime_v2.stages.mixing",
            "anime_v2.stages.export",
            "anime_v2.stages.mkv_export",
            "anime_v2.web.routes_jobs",
            "anime_v2.api.routes_auth",
            "anime_v2.api.routes_audit",
            "anime_v2.api.routes_keys",
            "anime_v2.api.routes_runtime",
            "anime_v2.jobs.queue",
            "anime_v2.realtime",
            "anime_v2.server",
            "anime_v2.web.app",
            "anime_v2.cli",
            "anime_v1.cli",
            "main",
    )

    try:
        for mod in modules:
            try:
                import_module(mod)
            except Exception as ex:
                print(f"IMPORT_SMOKE_FAILED: module={mod} error={ex}", file=sys.stderr)
                traceback.print_exc()
                return 2
    except Exception as ex:
        print(f"IMPORT_SMOKE_FAILED: error={ex}", file=sys.stderr)
        traceback.print_exc()
        return 2

    print("IMPORT_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
