#!/usr/bin/env python3
from __future__ import annotations

import math
import random
import tempfile
import wave
from pathlib import Path

from dubbing_pipeline.voice_refs.extract_refs import ExtractRefsConfig, extract_speaker_refs


def _write_wav(path: Path, *, sr: int, samples: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    b = bytearray()
    for v in samples:
        vv = max(-32768, min(32767, int(v)))
        b += int(vv).to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sr))
        wf.writeframes(bytes(b))


def _slice_wav(src: Path, *, start_s: float, end_s: float, out: Path) -> None:
    with wave.open(str(src), "rb") as wf:
        sr = int(wf.getframerate())
        n0 = max(0, int(start_s * sr))
        n1 = max(n0, int(end_s * sr))
        wf.setpos(n0)
        frames = wf.readframes(n1 - n0)
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as outwf:
        outwf.setnchannels(1)
        outwf.setsampwidth(2)
        outwf.setframerate(sr)
        outwf.writeframes(frames)


def _dur(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        return float(wf.getnframes()) / float(wf.getframerate() or 16000)


def main() -> int:
    random.seed(0)
    sr = 16000

    # Build a synthetic multi-speaker waveform:
    # - speaker A: loud sine
    # - speaker B: quieter sine
    # - overlap region: both at once (should be excluded)
    total_s = 12.0
    total_n = int(total_s * sr)
    a_freq = 440.0
    b_freq = 660.0
    samples: list[int] = []
    for i in range(total_n):
        t = i / sr
        x = 0.0
        # A: 0-4s and 8-12s
        if (0.0 <= t < 4.0) or (8.0 <= t < 12.0):
            x += 0.55 * math.sin(2 * math.pi * a_freq * t)
        # B: 4-8s
        if 4.0 <= t < 8.0:
            x += 0.25 * math.sin(2 * math.pi * b_freq * t)
        # overlap: 6-7s add A too (overlap with B)
        if 6.0 <= t < 7.0:
            x += 0.25 * math.sin(2 * math.pi * a_freq * t)
        # low-level noise in 9-10s (still speechy but noisier)
        if 9.0 <= t < 10.0:
            x += 0.03 * (random.random() * 2.0 - 1.0)
        samples.append(int(x * 32767.0))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        audio = root / "audio.wav"
        _write_wav(audio, sr=sr, samples=samples)

        # Create diarization-like segments with explicit overlap.
        # Note: in real jobs these are derived from diarization + mapping.
        segs = [
            {"start": 0.0, "end": 2.0, "speaker_id": "SPEAKER_01"},
            {"start": 2.0, "end": 4.0, "speaker_id": "SPEAKER_01"},
            {"start": 4.0, "end": 6.0, "speaker_id": "SPEAKER_02"},
            # overlap pair (should be excluded)
            {"start": 6.0, "end": 7.0, "speaker_id": "SPEAKER_02"},
            {"start": 6.0, "end": 7.0, "speaker_id": "SPEAKER_01"},
            {"start": 7.0, "end": 8.0, "speaker_id": "SPEAKER_02"},
            {"start": 8.0, "end": 10.0, "speaker_id": "SPEAKER_01"},
            {"start": 10.0, "end": 12.0, "speaker_id": "SPEAKER_01"},
        ]

        seg_dir = root / "segments"
        diar_segments: list[dict] = []
        for i, s in enumerate(segs):
            out = seg_dir / f"{i:04d}_{s['speaker_id']}.wav"
            _slice_wav(audio, start_s=float(s["start"]), end_s=float(s["end"]), out=out)
            diar_segments.append(
                {
                    "start": float(s["start"]),
                    "end": float(s["end"]),
                    "speaker_id": str(s["speaker_id"]),
                    "wav_path": str(out),
                }
            )

        out_dir = root / "voice_refs"
        cfg = ExtractRefsConfig(target_seconds=5.0, min_seg_seconds=0.5, max_seg_seconds=6.0, overlap_eps_s=0.01)
        man = extract_speaker_refs(diar_segments, audio, out_dir, cfg)

        items = man.get("items") or {}
        assert "SPEAKER_01" in items, items.keys()
        assert "SPEAKER_02" in items, items.keys()

        p1 = Path(items["SPEAKER_01"]["ref_path"])
        p2 = Path(items["SPEAKER_02"]["ref_path"])
        assert p1.exists() and p2.exists()

        d1 = _dur(p1)
        d2 = _dur(p2)
        assert 1.0 <= d1 <= 7.0, d1
        assert 1.0 <= d2 <= 7.0, d2

        # Ensure overlap region segments (6-7) are not used (start_s=6.0 in used list).
        used1 = items["SPEAKER_01"]["segments_used"]
        used2 = items["SPEAKER_02"]["segments_used"]
        for u in (used1 + used2):
            assert not (abs(float(u.get("start_s", -1.0)) - 6.0) < 1e-6 and abs(float(u.get("end_s", -1.0)) - 7.0) < 1e-6), u

    print("verify_voice_refs: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

