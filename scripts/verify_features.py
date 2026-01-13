#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@dataclass(frozen=True, slots=True)
class Feature:
    key: str
    name: str
    status: str
    evidence: list[str]


def _have(mod: str) -> bool:
    try:
        import_module(mod)
        return True
    except Exception:
        return False


def main() -> int:
    os.environ.setdefault("STRICT_SECRETS", "0")

    from config.settings import get_safe_config_report

    feats: list[Feature] = []

    # F1 Voice cloning / speaker preservation
    feats.append(
        Feature(
            "F1",
            "Voice cloning / speaker preservation",
            "Present",
            [
                "src/dubbing_pipeline/stages/tts.py (XTTS clone + preset fallbacks)",
                "src/dubbing_pipeline/web/routes_jobs.py (/api/jobs/{id}/characters voice map)",
            ],
        )
    )

    # F2 Emotion transfer / expressive control
    feats.append(
        Feature(
            "F2",
            "Emotion transfer / expressive speech control",
            "Present",
            [
                "src/dubbing_pipeline/stages/tts.py (_apply_prosody_ffmpeg + emotion_mode)",
                "src/dubbing_pipeline/cli.py (--emotion-mode/--speech-rate/--pitch/--energy)",
            ],
        )
    )

    # F3 Streaming/realtime
    feats.append(
        Feature(
            "F3",
            "Realtime / streaming dubbing",
            "Partial",
            [
                "src/dubbing_pipeline/realtime.py (pseudo-streaming chunk mode)",
                "src/dubbing_pipeline/cli.py (--realtime/--chunk-seconds/--chunk-overlap)",
            ],
        )
    )

    # F4 Web UI/API
    feats.append(
        Feature(
            "F4",
            "Web-based UI / API",
            "Present",
            [
                "src/dubbing_pipeline/server.py (FastAPI app + routers)",
                "src/dubbing_pipeline/web/ (web UI + job routes)",
            ],
        )
    )

    # F5 Multi-language
    feats.append(
        Feature(
            "F5",
            "Multi-language support",
            "Present",
            [
                "src/dubbing_pipeline/cli.py (--src-lang/--tgt-lang)",
                "src/dubbing_pipeline/stages/translation.py (multi-engine MT)",
            ],
        )
    )

    # F6 Alignment
    feats.append(
        Feature(
            "F6",
            "Timing & alignment precision",
            "Partial",
            [
                "src/dubbing_pipeline/stages/align.py (retime_tts + realign_srt)",
                "src/dubbing_pipeline/stages/transcription.py (optional word timestamps when supported)",
            ],
        )
    )

    # F7 Subtitles
    feats.append(
        Feature(
            "F7",
            "Subtitle generation (SRT/VTT)",
            "Present",
            [
                "src/dubbing_pipeline/utils/subtitles.py (write_srt/write_vtt)",
                "src/dubbing_pipeline/cli.py (--subs/--subs-format)",
            ],
        )
    )

    # F8 Batch
    feats.append(
        Feature(
            "F8",
            "Batch processing",
            "Present",
            [
                "src/dubbing_pipeline/web/routes_jobs.py (/api/jobs/batch)",
                "src/dubbing_pipeline/cli.py (--batch/--jobs/--resume/--fail-fast)",
            ],
        )
    )

    # F9 Providers / tuning hooks
    feats.append(
        Feature(
            "F9",
            "Model selection & fine-tuning hooks",
            "Partial",
            [
                "src/dubbing_pipeline/runtime/model_manager.py (model cache + device selection)",
                "scripts/train_voice.py (dataset manifest builder for optional training)",
                "src/dubbing_pipeline/stages/tts.py (tts_provider/voice_mode)",
            ],
        )
    )

    out = {
        "config": get_safe_config_report(),
        "features": [
            {
                "key": f.key,
                "name": f.name,
                "status": f.status,
                "evidence": f.evidence,
            }
            for f in feats
        ],
        "deps": {
            "fastapi": _have("fastapi"),
            "whisper": _have("whisper"),
            "TTS": _have("TTS"),
            "transformers": _have("transformers"),
            "aiortc": _have("aiortc"),
        },
    }
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
