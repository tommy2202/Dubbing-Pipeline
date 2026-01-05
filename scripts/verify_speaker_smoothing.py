from __future__ import annotations

import json
import math
import wave
from pathlib import Path

from config.settings import get_safe_config_report


def _write_wav(path: Path, *, seconds: float, sr: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = max(1, int(seconds * sr))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        for i in range(n):
            # 0.5s silence, 1.0s tone, 0.5s silence, 1.0s tone
            t = i / sr
            if t < 0.5 or (1.5 <= t < 2.0):
                v = 0
            else:
                v = int(0.2 * math.sin(2.0 * math.pi * (440.0 if t < 1.5 else 660.0) * t) * 32767)
            wf.writeframesraw(int(v).to_bytes(2, "little", signed=True))


def main() -> int:
    print("safe_config_report:", get_safe_config_report())
    from anime_v2.diarization.smoothing import (
        detect_scenes_audio,
        smooth_speakers_in_scenes,
        write_speaker_smoothing_report,
    )

    tmp = Path("_tmp_speaker_smoothing")
    tmp.mkdir(parents=True, exist_ok=True)
    wav = tmp / "audio.wav"
    _write_wav(wav, seconds=3.0)

    scenes = detect_scenes_audio(wav, min_scene_s=0.5, min_silence_s=0.3)
    assert scenes, "expected at least 1 scene"

    # Speaker flip micro-turn inside same scene: A, B(short), A.
    utts = [
        {"start": 0.55, "end": 0.95, "speaker": "SPEAKER_01", "conf": 0.9},
        {"start": 0.96, "end": 1.05, "speaker": "SPEAKER_02", "conf": 0.2},
        {"start": 1.06, "end": 1.40, "speaker": "SPEAKER_01", "conf": 0.9},
    ]
    smoothed, changes = smooth_speakers_in_scenes(
        utts, scenes, min_turn_s=0.25, surround_gap_s=0.2, conf_key="conf"
    )
    assert changes, "expected at least one smoothing change"
    assert smoothed[1]["speaker"] == "SPEAKER_01", "expected micro-turn to be merged into SPEAKER_01"

    outp = tmp / "analysis" / "speaker_smoothing.json"
    write_speaker_smoothing_report(
        outp,
        scenes=scenes,
        changes=changes,
        enabled=True,
        config={"min_turn_s": 0.25, "surround_gap_s": 0.2},
    )
    data = json.loads(outp.read_text(encoding="utf-8"))
    assert data.get("enabled") is True
    assert isinstance(data.get("changes"), list) and data["changes"]

    print("OK: verify_speaker_smoothing passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

