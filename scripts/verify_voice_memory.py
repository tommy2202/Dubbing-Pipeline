#!/usr/bin/env python3
from __future__ import annotations

import math
import os
import sys
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _write_tone(path: Path, *, hz: float, seconds: float = 0.6, sr: int = 16000) -> None:
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
    from tempfile import TemporaryDirectory

    from anime_v2.voice_memory.store import VoiceMemoryStore

    with TemporaryDirectory(prefix="anime_v2_voice_memory_") as td:
        root = Path(td) / "voice_memory"
        store = VoiceMemoryStore(root)

        a1 = Path(td) / "a1.wav"
        a2 = Path(td) / "a2.wav"
        b1 = Path(td) / "b1.wav"
        _write_tone(a1, hz=220.0)
        _write_tone(a2, hz=220.0)
        _write_tone(b1, hz=440.0)

        cid_a, sim_a, prov_a = store.match_or_create_from_wav(
            a1, device="cpu", threshold=0.75, auto_enroll=True
        )
        cid_a2, sim_a2, prov_a2 = store.match_or_create_from_wav(
            a2, device="cpu", threshold=0.75, auto_enroll=True
        )
        cid_b, sim_b, prov_b = store.match_or_create_from_wav(
            b1, device="cpu", threshold=0.75, auto_enroll=True
        )

        print("A1:", cid_a, sim_a, prov_a)
        print("A2:", cid_a2, sim_a2, prov_a2)
        print("B1:", cid_b, sim_b, prov_b)

        if cid_a != cid_a2:
            print("ERROR: speaker A did not match across runs", file=sys.stderr)
            return 2
        if cid_b == cid_a:
            print("ERROR: speaker B incorrectly matched speaker A", file=sys.stderr)
            return 2

        # ensure persistence on disk
        chars = store.list_characters()
        if len(chars) < 2:
            print("ERROR: expected at least 2 characters", file=sys.stderr)
            return 2

    print("VERIFY_VOICE_MEMORY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
