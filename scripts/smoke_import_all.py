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
            "dubbing_pipeline.config",
            "dubbing_pipeline.utils.ffmpeg_safe",
            "dubbing_pipeline.utils.log",
            "dubbing_pipeline.audio.separation",
            "dubbing_pipeline.audio.mix",
            "dubbing_pipeline.audio.tracks",
            "dubbing_pipeline.audio.music_detect",
            "dubbing_pipeline.text.pg_filter",
            "dubbing_pipeline.text.style_guide",
            "dubbing_pipeline.diarization.smoothing",
            "dubbing_pipeline.timing.fit_text",
            "dubbing_pipeline.timing.pacing",
            "dubbing_pipeline.voice_memory.store",
            "dubbing_pipeline.voice_memory.embeddings",
            "dubbing_pipeline.review.state",
            "dubbing_pipeline.review.ops",
            "dubbing_pipeline.review.cli",
            "dubbing_pipeline.qa.scoring",
            "dubbing_pipeline.qa.cli",
            "dubbing_pipeline.plugins.lipsync.base",
            "dubbing_pipeline.plugins.lipsync.registry",
            "dubbing_pipeline.plugins.lipsync.wav2lip_plugin",
            "dubbing_pipeline.expressive.prosody",
            "dubbing_pipeline.expressive.policy",
            "dubbing_pipeline.expressive.director",
            "dubbing_pipeline.streaming.chunker",
            "dubbing_pipeline.streaming.runner",
            "dubbing_pipeline.stages.audio_extractor",
            "dubbing_pipeline.stages.transcription",
            "dubbing_pipeline.stages.translation",
            "dubbing_pipeline.stages.diarization",
            "dubbing_pipeline.stages.tts",
            "dubbing_pipeline.stages.mixing",
            "dubbing_pipeline.stages.export",
            "dubbing_pipeline.stages.mkv_export",
            "dubbing_pipeline.web.routes_jobs",
            "dubbing_pipeline.api.routes_auth",
            "dubbing_pipeline.api.routes_audit",
            "dubbing_pipeline.api.routes_keys",
            "dubbing_pipeline.api.routes_runtime",
            "dubbing_pipeline.jobs.queue",
            "dubbing_pipeline.realtime",
            "dubbing_pipeline.server",
            "dubbing_pipeline.web.app",
            "dubbing_pipeline.cli",
            "dubbing_pipeline_legacy.cli",
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
