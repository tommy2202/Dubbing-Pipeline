from __future__ import annotations

import json
import tempfile
from pathlib import Path


def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")


def _touch_wav(path: Path) -> None:
    # tiny valid PCM wav for refs
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 1600)


def main() -> int:
    from dubbing_pipeline.voice_memory.audition import audition
    from dubbing_pipeline.voice_memory.store import VoiceMemoryStore
    from dubbing_pipeline.voice_memory.tools import merge_characters, undo_merge

    with tempfile.TemporaryDirectory(prefix="verify_voice_tools_") as td:
        root = Path(td) / "voice_memory"
        store = VoiceMemoryStore(root)

        # Setup two characters
        a = store.ensure_character(character_id="SPEAKER_01")
        b = store.ensure_character(character_id="SPEAKER_02")
        _touch_wav(store.character_dir(a) / "ref_001.wav")
        _touch_wav(store.character_dir(b) / "ref_001.wav")

        # One episode mapping referencing from_id
        store.write_episode_mapping(
            "ep_test",
            source={"note": "test"},
            mapping={"spk0": {"character_id": a, "similarity": 0.9, "provider": "x", "confidence": 0.8}},
        )

        # Merge A -> B
        backup = merge_characters(store_root=root, from_id=a, to_id=b, move_refs=False, keep_alias=True)
        assert (root / "backups" / backup.merge_id / "manifest.json").exists()

        # mapping updated
        ep = json.loads((root / "episodes" / "ep_test.json").read_text(encoding="utf-8"))
        assert ep["mapping"]["spk0"]["character_id"] == b

        # Undo merge
        undo_merge(store_root=root, merge_id=backup.merge_id)
        ep2 = json.loads((root / "episodes" / "ep_test.json").read_text(encoding="utf-8"))
        assert ep2["mapping"]["spk0"]["character_id"] == a

        # Audition writes wavs even if TTS unavailable (silence fallback)
        out_job = Path(td) / "Output" / "job_aud"
        man = audition(text="Hello world.", top_n=1, character_id=None, out_job_dir=out_job, language="en")
        assert (out_job / "audition" / "manifest.json").exists()
        wavs = [Path(r["wav"]) for r in man.get("results", [])]
        assert wavs and wavs[0].exists()

    print("verify_voice_tools: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

