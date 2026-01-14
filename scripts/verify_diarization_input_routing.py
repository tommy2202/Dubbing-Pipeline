#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import wave
from pathlib import Path

from dubbing_pipeline.audio.routing import resolve_diarization_input
from dubbing_pipeline.jobs.models import Job, JobState


def _write_tiny_wav(path: Path, *, sr: int = 16000, seconds: float = 0.2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(sr * float(seconds))
    frames = (b"\x00\x00") * n
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(frames)


def _mk_job(*, job_id: str, base_dir: Path) -> Job:
    # Minimal Job instance for routing tests; only id/work_dir are required for base_dir resolution.
    return Job(
        id=str(job_id),
        owner_id="u",
        video_path=str(base_dir / "Input" / "x.mp4"),
        src_lang="en",
        tgt_lang="en",
        mode="medium",
        device="cpu",
        state=JobState.QUEUED,
        progress=0.0,
        message="",
        error=None,
        created_at="",
        updated_at="",
        runtime={},
        output_mkv="",
        output_srt="",
        work_dir=str(base_dir),
        log_path="",
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        base = (root / "Output" / "job").resolve()
        base.mkdir(parents=True, exist_ok=True)
        stems = base / "stems"
        stems.mkdir(parents=True, exist_ok=True)
        extracted = base / "work" / "jid" / "audio.wav"
        _write_tiny_wav(extracted)

        job = _mk_job(job_id="jid", base_dir=base)

        # Case 1: separation enabled + dialogue stem exists -> dialogue stem
        dlg = stems / "dialogue.wav"
        _write_tiny_wav(dlg)
        r1 = resolve_diarization_input(job, extracted_wav=extracted, base_dir=base, separation_enabled=True)
        assert r1.kind == "dialogue_stem", r1
        assert Path(r1.wav) == dlg.resolve(), r1
        assert r1.rel_path.replace("\\", "/") == "stems/dialogue.wav", r1.rel_path

        # Case 2: separation enabled but dialogue stem missing -> original
        dlg.unlink(missing_ok=True)
        r2 = resolve_diarization_input(job, extracted_wav=extracted, base_dir=base, separation_enabled=True)
        assert r2.kind == "original", r2
        assert Path(r2.wav) == extracted.resolve(), r2
        assert "audio.wav" in r2.rel_path, r2.rel_path

        # Case 3: separation disabled even if dialogue stem exists -> original
        _write_tiny_wav(dlg)
        r3 = resolve_diarization_input(job, extracted_wav=extracted, base_dir=base, separation_enabled=False)
        assert r3.kind == "original", r3
        assert Path(r3.wav) == extracted.resolve(), r3

    print("verify_diarization_input_routing: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

