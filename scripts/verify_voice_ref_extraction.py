#!/usr/bin/env python3
from __future__ import annotations

import math
import random
import shutil
import tempfile
import wave
from pathlib import Path


def _need_tool(name: str) -> None:
    if shutil.which(name):
        return
    raise RuntimeError(f"Missing required tool: {name}")


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


def _dur(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        return float(wf.getnframes()) / float(wf.getframerate() or 16000)


def main() -> int:
    # This verifier uses ffmpeg slicing via extract_audio_mono_16k, so require ffmpeg.
    _need_tool("ffmpeg")
    _need_tool("ffprobe")

    from dubbing_pipeline.voice_refs.extract_refs import ExtractRefsConfig, extract_speaker_refs

    random.seed(0)
    sr = 16000
    total_s = 12.0
    total_n = int(total_s * sr)
    a_freq = 440.0
    b_freq = 660.0
    samples: list[int] = []
    for i in range(total_n):
        t = i / sr
        x = 0.0
        # Speaker 01: 0-4, 8-12 (clean)
        if (0.0 <= t < 4.0) or (8.0 <= t < 12.0):
            x += 0.55 * math.sin(2 * math.pi * a_freq * t)
        # Speaker 02: 4-8 (quieter)
        if 4.0 <= t < 8.0:
            x += 0.25 * math.sin(2 * math.pi * b_freq * t)
        # Overlap region 6-7: add speaker 01 too (should be excluded)
        if 6.0 <= t < 7.0:
            x += 0.25 * math.sin(2 * math.pi * a_freq * t)
        # Noise burst 9-10 (should be penalized)
        if 9.0 <= t < 10.0:
            x += 0.06 * (random.random() * 2.0 - 1.0)
        samples.append(int(x * 32767.0))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        dialogue = root / "dialogue.wav"
        _write_wav(dialogue, sr=sr, samples=samples)

        # Fake diarization timeline (with explicit overlap and a too-short segment).
        tl = [
            {"start": 0.0, "end": 2.0, "speaker_id": "SPEAKER_01"},
            {"start": 2.0, "end": 4.0, "speaker_id": "SPEAKER_01"},
            {"start": 4.0, "end": 6.0, "speaker_id": "SPEAKER_02"},
            # too short (should be rejected by min_seg_seconds=2)
            {"start": 7.9, "end": 8.8, "speaker_id": "SPEAKER_02"},
            # overlap pair (should be excluded)
            {"start": 6.0, "end": 7.0, "speaker_id": "SPEAKER_02"},
            {"start": 6.0, "end": 7.0, "speaker_id": "SPEAKER_01"},
            {"start": 8.0, "end": 10.0, "speaker_id": "SPEAKER_01"},
            {"start": 10.0, "end": 12.0, "speaker_id": "SPEAKER_01"},
        ]

        out_dir = root / "out"
        cfg = ExtractRefsConfig(target_seconds=5.0, min_seg_seconds=2.0, max_seg_seconds=10.0, overlap_eps_s=0.01)
        man = extract_speaker_refs(tl, dialogue, out_dir, cfg)

        items = man.get("items") if isinstance(man, dict) else None
        assert isinstance(items, dict) and items, "missing items"
        assert "SPEAKER_01" in items and "SPEAKER_02" in items

        p1 = Path(str(items["SPEAKER_01"]["ref_path"]))
        p2 = Path(str(items["SPEAKER_02"]["ref_path"]))
        assert p1.exists() and p2.exists()

        d1 = _dur(p1)
        d2 = _dur(p2)
        assert 1.0 <= d1 <= 8.0, d1
        assert 1.0 <= d2 <= 8.0, d2

        # Ensure overlap segments (6-7) are not listed as used
        used1 = items["SPEAKER_01"]["segments_used"]
        used2 = items["SPEAKER_02"]["segments_used"]
        for u in (used1 + used2):
            st = float(u.get("start_s", -1.0))
            en = float(u.get("end_s", -1.0))
            assert not (abs(st - 6.0) < 1e-6 and abs(en - 7.0) < 1e-6), u

    print("verify_voice_ref_extraction: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

