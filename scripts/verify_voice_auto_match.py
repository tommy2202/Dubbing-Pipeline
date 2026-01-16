#!/usr/bin/env python3
from __future__ import annotations

import math
import os
import sys
import tempfile
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _write_tone(path: Path, *, hz: float, seconds: float = 0.8, sr: int = 16000) -> None:
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
    from dubbing_pipeline.voice_store.embeddings import suggest_matches
    from dubbing_pipeline.voice_store.store import save_character_ref

    with tempfile.TemporaryDirectory(prefix="verify_voice_auto_match_") as td:
        root = Path(td).resolve()
        voice_store_dir = root / "voice_store"
        series_slug = "series_one"

        char_a = root / "alice.wav"
        char_b = root / "bob.wav"
        speaker = root / "speaker.wav"
        _write_tone(char_a, hz=220.0)
        _write_tone(char_b, hz=440.0)
        _write_tone(speaker, hz=220.0)

        save_character_ref(
            series_slug,
            "alice",
            char_a,
            job_id="job1",
            metadata={"display_name": "Alice", "created_by": "verify"},
            voice_store_dir=voice_store_dir,
        )
        save_character_ref(
            series_slug,
            "bob",
            char_b,
            job_id="job2",
            metadata={"display_name": "Bob", "created_by": "verify"},
            voice_store_dir=voice_store_dir,
        )

        matches = suggest_matches(
            series_slug=series_slug,
            speaker_refs={"SPEAKER_01": speaker},
            threshold=0.0,
            device="cpu",
            voice_store_dir=voice_store_dir,
            allow_fingerprint=True,
        )
        if not matches:
            print("SKIP: embeddings unavailable")
            return 0

        best = {m.get("speaker_id"): m for m in matches}.get("SPEAKER_01")
        if not best:
            print("ERROR: no match for speaker", file=sys.stderr)
            return 2
        if str(best.get("provider") or "") != "fingerprint":
            print(f"SKIP: provider={best.get('provider')}")
            return 0
        if str(best.get("character_slug") or "") != "alice":
            print("ERROR: expected match to 'alice'", file=sys.stderr)
            return 2

    print("VERIFY_VOICE_AUTO_MATCH_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
