#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
import wave
from pathlib import Path


def _write_silence_wav(path: Path, *, seconds: float = 1.0, sr: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = max(1, int(seconds * sr))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b"\x00\x00" * n)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="dp_verify_voice_store_") as td:
        root = Path(td).resolve()
        os.chdir(root)
        store_root = (root / "voice_store").resolve()
        os.environ["VOICE_STORE"] = str(store_root)

        from dubbing_pipeline.voice_store.store import (
            delete_character,
            get_character_ref,
            get_series_root,
            list_characters,
            save_character_ref,
        )

        series = "tensura"
        character = "rimuru"
        src = root / "ref.wav"
        _write_silence_wav(src, seconds=1.0)

        out = save_character_ref(
            series,
            character,
            src,
            job_id="job123",
            metadata={"display_name": "Rimuru", "created_by": "user1"},
        )
        if not out.exists():
            raise RuntimeError("ref.wav not created")

        sr = get_series_root(series)
        idx = sr / "index.json"
        if not idx.exists():
            raise RuntimeError("missing index.json")
        data = json.loads(idx.read_text(encoding="utf-8"))
        if str(data.get("series_slug")) != series:
            raise RuntimeError("index.json series_slug mismatch")

        chars = list_characters(series)
        if not any(c.get("character_slug") == character for c in chars):
            raise RuntimeError("character not listed in index.json")

        cref = get_character_ref(series, character)
        if cref is None or not cref.exists():
            raise RuntimeError("get_character_ref returned None/missing")

        # delete should remove directory + index entry
        if not delete_character(series, character):
            raise RuntimeError("delete_character returned False")
        if get_character_ref(series, character) is not None:
            raise RuntimeError("character ref still exists after delete")

    print("[ok] verify_voice_store_layout")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

