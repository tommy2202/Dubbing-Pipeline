#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _write_silence(path: Path, *, seconds: float, sr: int = 16000) -> None:
    n = int(sr * seconds)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b"\x00\x00" * n)


def main() -> int:
    os.environ.setdefault("STRICT_SECRETS", "0")
    from tempfile import TemporaryDirectory

    from dubbing_pipeline.timing.pacing import match_segment_duration, measure_wav_seconds

    with TemporaryDirectory(prefix="dubbing_pipeline_pacing_") as td:
        td_p = Path(td)
        long_wav = td_p / "long.wav"
        short_wav = td_p / "short.wav"
        _write_silence(long_wav, seconds=1.5)
        _write_silence(short_wav, seconds=0.5)

        out1, rep1 = match_segment_duration(
            long_wav, target_seconds=1.0, tolerance=0.05, min_ratio=0.88, max_ratio=1.18
        )
        out2, rep2 = match_segment_duration(
            short_wav, target_seconds=1.0, tolerance=0.05, min_ratio=0.88, max_ratio=1.18
        )

        d1 = measure_wav_seconds(out1)
        d2 = measure_wav_seconds(out2)
        print("long->", rep1.to_dict(), "dur=", d1)
        print("short->", rep2.to_dict(), "dur=", d2)

        if abs(d2 - 1.0) > 0.10:
            print("ERROR: pad did not reach target", file=sys.stderr)
            return 2
        if d1 <= 0:
            print("ERROR: stretch/trim produced invalid duration", file=sys.stderr)
            return 2

    print("SMOKE_SEGMENT_PACING_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
