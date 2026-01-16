from __future__ import annotations

import json
import shutil
import tempfile
import wave
from pathlib import Path


def _write_silence_wav(path: Path, duration_s: float, sr: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = max(1, int(duration_s * sr))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b"\x00\x00" * n)


def main() -> int:
    try:
        from dubbing_pipeline.review.ops import edit_segment, lock_segment, regen_segment
        from dubbing_pipeline.review.state import (
            init_state_from_job,
            load_state,
            render_audio_only,
            save_state,
        )
    except Exception as ex:
        print(f"Import failed: {ex}")
        return 2

    tmp_root = Path(tempfile.mkdtemp(prefix="verify_review_loop_"))
    try:
        job_dir = tmp_root / "Output" / "JOB_TEST"
        job_dir.mkdir(parents=True, exist_ok=True)

        # Minimal translated.json with 2 segments.
        translated = {
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.2,
                    "speaker": "SPEAKER_01",
                    "src_text": "こんにちは",
                    "text": "Hello there.",
                },
                {
                    "start": 1.5,
                    "end": 2.2,
                    "speaker": "SPEAKER_02",
                    "src_text": "元気？",
                    "text": "How are you?",
                },
            ]
        }
        (job_dir / "translated.json").write_text(json.dumps(translated, indent=2), encoding="utf-8")

        # Seed "original" clips so state init has audio paths.
        _write_silence_wav(job_dir / "tts_clips" / "0000_SPEAKER_01.wav", 1.2)
        _write_silence_wav(job_dir / "tts_clips" / "0001_SPEAKER_02.wav", 0.7)

        state = init_state_from_job(job_dir=job_dir, video_path=None, pipeline_params={}, voice_mapping_snapshot={})
        save_state(job_dir, state)

        # Edit + regen a segment
        edit_segment(job_dir, 1, text="Hello (edited).")
        p = regen_segment(job_dir, 1)
        if not p.exists() or p.stat().st_size == 0:
            print("regen failed: output missing/empty")
            return 1

        # Lock should succeed now
        lock_segment(job_dir, 1)

        # Render audio-only should succeed (uses latest state on disk)
        out_wav = job_dir / "review" / "review_render.wav"
        render_audio_only(job_dir, state=load_state(job_dir), out_wav=out_wav)
        if not out_wav.exists() or out_wav.stat().st_size == 0:
            print("render failed: output missing/empty")
            return 1

        print("OK")
        return 0
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

