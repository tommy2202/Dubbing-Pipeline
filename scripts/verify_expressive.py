from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> int:
    try:
        from dubbing_pipeline.expressive.policy import plan_for_segment
        from dubbing_pipeline.expressive.prosody import analyze_segment
    except Exception as ex:
        print(f"IMPORT_FAILED: {ex}")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="verify_expressive_"))
    try:
        wav = tmp / "src.wav"
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            # No ffmpeg: policy should still work (text-only)
            plan = plan_for_segment(
                segment_id=1,
                mode="text-only",
                strength=0.5,
                text="Hello... are you ok?!",
                features=None,
            )
            print(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
            return 0

        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=220:duration=1",
                "-ac",
                "1",
                "-ar",
                "16000",
                str(wav),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        seg_wav = tmp / "seg.wav"
        feats = analyze_segment(
            source_audio_wav=wav,
            start_s=0.0,
            end_s=1.0,
            text="Hello!",
            out_wav=seg_wav,
            pitch=True,
        )
        plan = plan_for_segment(
            segment_id=1,
            mode="source-audio",
            strength=0.5,
            text="Hello!",
            features=feats,
        )
        print(json.dumps({"features": feats.to_dict(), "plan": plan.to_dict()}, indent=2, sort_keys=True))
        return 0
    except Exception as ex:
        print(f"VERIFY_EXPRESSIVE_FAILED: {ex}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

