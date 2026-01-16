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
    with tempfile.TemporaryDirectory(prefix="dp_verify_char_ref_") as td:
        root = Path(td).resolve()
        os.chdir(root)

        os.environ["VOICE_STORE"] = str((root / "voice_store").resolve())
        # Keep artifacts local to temp dir
        os.environ["DUBBING_OUTPUT_DIR"] = str((root / "Output").resolve())
        os.environ["DUBBING_STATE_DIR"] = str((root / "state").resolve())

        from dubbing_pipeline.stages import tts
        from dubbing_pipeline.voice_store.store import save_character_ref

        series_slug = "tensura"
        speaker_id = "SPEAKER_00"
        character_slug = "rimuru"

        # Create persistent character ref
        char_ref_src = root / "char_ref.wav"
        _write_silence_wav(char_ref_src, seconds=1.0)
        save_character_ref(
            series_slug,
            character_slug,
            char_ref_src,
            job_id="jobX",
            metadata={"display_name": "Rimuru", "created_by": "user1"},
        )

        # Create this-job extracted speaker ref under voice_ref_dir
        voice_ref_dir = root / "job_voice_refs"
        voice_ref_dir.mkdir(parents=True, exist_ok=True)
        spk_ref = voice_ref_dir / f"{speaker_id}.wav"
        _write_silence_wav(spk_ref, seconds=1.0)

        # Minimal translated.json with one segment
        out_dir = root / "work"
        out_dir.mkdir(parents=True, exist_ok=True)
        translated_json = out_dir / "translated.json"
        translated_json.write_text(
            json.dumps(
                {
                    "segments": [
                        {
                            "start": 0.0,
                            "end": 1.0,
                            "text": "hello",
                            "speaker_id": speaker_id,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        wav = tts.run(
            out_dir=out_dir,
            translated_json=translated_json,
            voice_mode="clone",
            no_clone=False,
            series_slug=series_slug,
            speaker_character_map={speaker_id: character_slug},
            voice_ref_dir=voice_ref_dir,
            voice_store_dir=Path(os.environ["VOICE_STORE"]),
            # opt-in for auto-enroll (should be no-op since ref already exists)
            voice_memory=True,
            job_id="jobX",
        )
        if not wav.exists():
            raise RuntimeError("tts did not produce wav output")

        man = json.loads((out_dir / "tts_manifest.json").read_text(encoding="utf-8"))
        rep = man.get("speaker_report")
        if not isinstance(rep, dict):
            raise RuntimeError("missing speaker_report")
        r0 = rep.get(speaker_id)
        if not isinstance(r0, dict):
            raise RuntimeError("missing speaker report for speaker")
        refs_used = r0.get("refs_used")
        if not isinstance(refs_used, list) or not refs_used:
            raise RuntimeError("refs_used missing/empty")
        # character ref should be preferred (present)
        if not any(character_slug in str(p) for p in refs_used):
            raise RuntimeError(f"expected character ref in refs_used, got {refs_used}")

    print("[ok] verify_character_ref_resolution")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

