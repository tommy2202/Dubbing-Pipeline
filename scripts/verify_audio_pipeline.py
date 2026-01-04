#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _write_tone(path: Path, *, sr: int = 48000, hz: float = 440.0, seconds: float = 0.5) -> None:
    import math

    n = int(sr * seconds)
    frames = bytearray()
    for i in range(n):
        x = math.sin(2.0 * math.pi * hz * (i / sr))
        s16 = int(max(-1.0, min(1.0, x)) * 12000)
        frames += int(s16).to_bytes(2, byteorder="little", signed=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(bytes(frames))


def main() -> int:
    os.environ.setdefault("STRICT_SECRETS", "0")
    os.environ.setdefault("OFFLINE_MODE", "1")
    os.environ.setdefault("ALLOW_EGRESS", "0")
    os.environ.setdefault("ALLOW_HF_EGRESS", "0")

    from config.settings import get_safe_config_report

    print("SAFE_CONFIG_REPORT:")
    print(json.dumps(get_safe_config_report(), indent=2, sort_keys=True))

    # ffmpeg availability (PATH or configured)
    try:
        from anime_v2.utils.ffmpeg_safe import ffprobe_duration_seconds

        sample = REPO_ROOT / "samples" / "sample.mp4"
        if sample.exists():
            _ = ffprobe_duration_seconds(sample, timeout_s=10)
    except Exception as ex:
        print(f"ERROR: ffmpeg/ffprobe check failed: {ex}", file=sys.stderr)
        return 2

    # demucs optional availability
    try:
        from anime_v2.audio.separation import demucs_available

        print(f"DEMUX_AVAILABLE={bool(demucs_available())}")
    except Exception as ex:
        print(f"ERROR: demucs availability check failed: {ex}", file=sys.stderr)
        return 2

    # Dry-run enhanced mix on tiny WAVs (no video needed)
    try:
        from tempfile import TemporaryDirectory

        from anime_v2.audio.mix import MixParams, mix_dubbed_audio

        with TemporaryDirectory(prefix="anime_v2_audio_verify_") as td:
            td_p = Path(td)
            bg = td_p / "bg.wav"
            tts = td_p / "tts.wav"
            out = td_p / "final_mix.wav"
            _write_tone(bg, hz=220.0, seconds=0.6)
            _write_tone(tts, hz=440.0, seconds=0.5)
            mix_dubbed_audio(
                background_wav=bg,
                tts_dialogue_wav=tts,
                out_wav=out,
                params=MixParams(
                    lufs_target=-16.0, ducking=True, ducking_strength=1.0, limiter=True
                ),
                timeout_s=60,
            )
            if not out.exists() or out.stat().st_size < 1000:
                raise RuntimeError("mix output missing or too small")
    except Exception as ex:
        print(f"ERROR: mix dry-run failed: {ex}", file=sys.stderr)
        return 2

    print("VERIFY_AUDIO_PIPELINE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
