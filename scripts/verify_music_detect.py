from __future__ import annotations

import math
import sys
import wave
from pathlib import Path

from config.settings import get_safe_config_report


def _which_ok(p: str) -> bool:
    import shutil

    return bool(shutil.which(p))


def _rms_window(path: Path, *, start_s: float, end_s: float) -> float:
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        n0 = int(max(0.0, start_s) * sr)
        n1 = int(max(0.0, end_s) * sr)
        n1 = max(n0 + 1, min(n1, wf.getnframes()))
        wf.setpos(min(n0, max(0, wf.getnframes() - 1)))
        buf = wf.readframes(n1 - n0)
    n = len(buf) // 2
    if n <= 0:
        return 0.0
    s2 = 0.0
    for i in range(0, n * 2, 2):
        v = int.from_bytes(buf[i : i + 2], "little", signed=True)
        x = float(v) / 32768.0
        s2 += x * x
    return math.sqrt(s2 / float(n))


def _duration_s(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
    return float(n) / float(sr) if sr else 0.0


def main() -> int:
    print("safe_config_report:", get_safe_config_report())

    if not _which_ok("ffmpeg") or not _which_ok("ffprobe"):
        print("ERROR: ffmpeg/ffprobe not found on PATH", file=sys.stderr)
        return 2

    # Local imports (keep import-time light)
    from dubbing_pipeline.audio.music_detect import (
        analyze_audio_for_music_regions,
        build_music_preserving_bed,
        should_suppress_segment,
    )

    tmp = Path("_tmp_music_detect")
    tmp.mkdir(parents=True, exist_ok=True)

    speech = tmp / "speech.wav"
    music = tmp / "music.wav"
    full = tmp / "full.wav"
    silence = tmp / "silence.wav"
    bed = tmp / "bed.wav"

    import subprocess

    def sh(cmd: list[str]) -> None:
        subprocess.run(cmd, check=True, capture_output=True)

    # 1s "speech-ish" band-limited noise
    sh(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=d=1:c=white",
            "-filter:a",
            "highpass=f=300,lowpass=f=3000,volume=0.25",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(speech),
        ]
    )
    # 1s "music-ish" two-tone mix
    sh(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=f=440:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=f=660:d=1",
            "-filter_complex",
            "[0:a][1:a]amix=inputs=2:normalize=0,volume=0.25",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(music),
        ]
    )
    sh(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(speech),
            "-i",
            str(music),
            "-filter_complex",
            "[0:a][1:a]concat=n=2:v=0:a=1",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(full),
        ]
    )
    sh(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono:d=2",
            "-c:a",
            "pcm_s16le",
            str(silence),
        ]
    )

    regs = analyze_audio_for_music_regions(full, mode="heuristic", threshold=0.55)
    print("regions:", [r.to_dict() for r in regs])
    if not regs:
        print("ERROR: expected at least one region", file=sys.stderr)
        return 3

    # Expect at least some overlap with the 1..2s region.
    overlaps_music = any((r.end > 1.0 and r.start < 2.0) for r in regs)
    if not overlaps_music:
        print("ERROR: expected a region overlapping ~[1..2] seconds", file=sys.stderr)
        return 4

    # Segment suppression check
    if not should_suppress_segment(1.2, 1.4, [r.to_dict() for r in regs]):
        print("ERROR: expected suppression for segment inside music region", file=sys.stderr)
        return 5

    # Bed builder check: background silent, original has content. Bed should contain content only in music region.
    build_music_preserving_bed(
        background_wav=silence,
        original_wav=full,
        regions=[r.to_dict() for r in regs],
        out_wav=bed,
    )
    dur = _duration_s(bed)
    r0 = regs[0]
    inside_a = max(0.0, float(r0.start) + 0.1)
    inside_b = min(dur, inside_a + 0.4)
    outside_a = min(max(0.0, float(r0.end) + 0.1), max(0.0, dur - 0.5))
    outside_b = min(dur, outside_a + 0.4)
    rms_inside = _rms_window(bed, start_s=inside_a, end_s=inside_b)
    rms_outside = _rms_window(bed, start_s=outside_a, end_s=outside_b) if outside_b > outside_a else 0.0
    print(
        "bed_rms_inside:",
        rms_inside,
        "bed_rms_outside:",
        rms_outside,
        "inside_window:",
        (inside_a, inside_b),
        "outside_window:",
        (outside_a, outside_b),
    )
    if rms_inside <= max(0.01, 5.0 * rms_outside):
        print("ERROR: expected bed to preserve original music energy", file=sys.stderr)
        return 6

    print("OK: verify_music_detect passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

