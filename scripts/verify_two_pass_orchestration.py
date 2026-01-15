#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
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


def _ffmpeg(*args: str) -> None:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg not found on PATH")
    cmd = [exe, "-y", *args]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(cmd)}\n{p.stderr}")


async def _run_pass2_once(tmp_root: Path) -> None:
    out_dir = (tmp_root / "Output").resolve()
    state_dir = (tmp_root / "state").resolve()
    inp_dir = (tmp_root / "Input").resolve()
    inp_dir.mkdir(parents=True, exist_ok=True)

    os.environ["DUBBING_OUTPUT_DIR"] = str(out_dir)
    os.environ["DUBBING_STATE_DIR"] = str(state_dir)
    # Ensure pass2 does NOT execute any earlier stage functions.
    os.environ["DUBBING_PIPELINE_FORBID_STAGES"] = ",".join(
        ["audio_extractor", "separation", "diarize", "transcribe", "translate"]
    )

    # Import after env vars are set so settings pick them up.
    from dubbing_pipeline.jobs.checkpoint import read_ckpt, write_ckpt
    from dubbing_pipeline.jobs.models import Job, JobState, now_utc
    from dubbing_pipeline.jobs.queue import JobQueue
    from dubbing_pipeline.jobs.store import JobStore
    from dubbing_pipeline.library.paths import get_job_output_root

    video = inp_dir / "verify_two_pass.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=320x240:r=24:d=2",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=mono:sample_rate=16000",
        "-shortest",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(video),
    )

    store = JobStore(db_path=(state_dir / "jobs.db").resolve())
    job_id = "verify-two-pass-job"

    job = Job(
        id=job_id,
        owner_id="verify",
        video_path=str(video),
        duration_s=2.0,
        mode="high",
        device="auto",
        src_lang="ja",
        tgt_lang="en",
        created_at=now_utc(),
        updated_at=now_utc(),
        state=JobState.QUEUED,
        progress=0.0,
        message="queued",
        output_mkv="",
        output_srt="",
        work_dir="",
        log_path="",
        runtime={
            "source_stem": "verify_two_pass",
            "voice_clone_two_pass": True,
            "two_pass": {"phase": "pass2", "request": "rerun_pass2"},
        },
    )
    store.put(job)

    base_dir = get_job_output_root(job)
    stem = base_dir.name
    work_dir = (base_dir / "work" / job_id).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    # Artifacts required by pass2 checkpoint checks.
    wav = work_dir / "audio.wav"
    _write_silence_wav(wav, seconds=2.0)

    srt_out = work_dir / f"{stem}.srt"
    srt_meta = srt_out.with_suffix(".json")
    srt_out.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nhello\n\n",
        encoding="utf-8",
    )
    srt_meta.write_text(json.dumps({"imported": True}), encoding="utf-8")

    translated_json = base_dir / "translated.json"
    translated_srt = base_dir / f"{stem}.translated.srt"
    translated_json.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "start": 0.0,
                        "end": 1.0,
                        "text": "hello",
                        "speaker_id": "SPEAKER_00",
                        "src_text": "こんにちは",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    translated_srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nhello\n\n",
        encoding="utf-8",
    )

    # Provide a per-speaker extracted ref in the job-local analysis location.
    refs_dir = (base_dir / "analysis" / "voice_refs").resolve()
    refs_dir.mkdir(parents=True, exist_ok=True)
    ref_wav = refs_dir / "SPEAKER_00.wav"
    _write_silence_wav(ref_wav, seconds=1.0)

    ckpt_path = base_dir / ".checkpoint.json"
    write_ckpt(job_id, "audio", {"audio_wav": wav}, {"work_dir": str(work_dir)}, ckpt_path=ckpt_path)
    write_ckpt(
        job_id,
        "transcribe",
        {"srt_out": srt_out, "srt_meta": srt_meta},
        {"work_dir": str(work_dir)},
        ckpt_path=ckpt_path,
    )
    write_ckpt(
        job_id,
        "translate",
        {"translated_json": translated_json, "translated_srt": translated_srt},
        {"work_dir": str(work_dir)},
        ckpt_path=ckpt_path,
    )
    # Ensure checkpoints are readable.
    if not read_ckpt(job_id, ckpt_path=ckpt_path):
        raise RuntimeError("failed to read checkpoint we just wrote")

    q = JobQueue(store, concurrency=1, app_root=tmp_root)
    await q._run_job(job_id)  # intentional: orchestration verifier

    tts_manifest = (base_dir / "analysis" / "tts_manifest.json").resolve()
    if not tts_manifest.exists():
        items = []
        try:
            items = sorted(p.name for p in (base_dir / "analysis").iterdir())
        except Exception:
            items = []
        raise RuntimeError(
            "missing tts_manifest.json (pass2 did not run TTS or artifact was pruned). "
            f"analysis_dir={(base_dir / 'analysis').resolve()} entries={items}"
        )
    man = json.loads(tts_manifest.read_text(encoding="utf-8"))
    rep = man.get("speaker_report") if isinstance(man, dict) else None
    if not isinstance(rep, dict):
        raise RuntimeError("tts_manifest.json missing speaker_report")

    # Assert TTS consumed the extracted per-speaker ref (even if XTTS fell back).
    sp = rep.get("SPEAKER_00")
    if not isinstance(sp, dict):
        raise RuntimeError("speaker_report missing SPEAKER_00")
    refs_used = sp.get("refs_used")
    if not isinstance(refs_used, list) or not refs_used:
        raise RuntimeError("speaker_report.SPEAKER_00.refs_used missing/empty (ref not consumed)")
    refs_used_paths = [str(Path(str(p)).resolve()) for p in refs_used if str(p).strip()]
    if str(ref_wav.resolve()) not in refs_used_paths:
        raise RuntimeError(f"expected refs_used to include {ref_wav} but got {refs_used}")

    log_tail = store.tail_log(job_id, n=400)
    if "passB_cloning_started" not in log_tail:
        raise RuntimeError("missing passB_cloning_started marker in job log")
    if "passB_complete" not in log_tail:
        raise RuntimeError("missing passB_complete marker in job log")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="dp_verify_two_pass_") as td:
        tmp_root = Path(td).resolve()
        try:
            asyncio.run(_run_pass2_once(tmp_root))
        except Exception as ex:
            msg = str(ex)
            # Skip only if ffmpeg is missing (common on minimal runners).
            if "ffmpeg not found" in msg:
                print(f"[skip] {msg}")
                return 0
            print(f"[fail] {ex}")
            return 1
    print("[ok] verify_two_pass_orchestration")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

