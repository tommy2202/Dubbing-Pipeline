from __future__ import annotations

import asyncio
import os
import wave
from pathlib import Path


def _write_silence_wav(path: Path, *, seconds: float = 1.0, sr: int = 16000) -> None:
    n = int(float(seconds) * sr)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b"\x00\x00" * n)


def main() -> int:
    out = Path("/tmp/anime_v2_verify_import").resolve()
    out.mkdir(parents=True, exist_ok=True)
    os.environ["APP_ROOT"] = "/workspace"
    os.environ["ANIME_V2_OUTPUT_DIR"] = str(out)
    os.environ["ANIME_V2_LOG_DIR"] = str(out / "logs")
    os.environ["COOKIE_SECURE"] = "0"

    # Very small timeouts (we stub heavy stages)
    os.environ["WATCHDOG_AUDIO_S"] = "10"
    os.environ["WATCHDOG_WHISPER_S"] = "10"
    os.environ["WATCHDOG_TRANSLATE_S"] = "10"
    os.environ["WATCHDOG_TTS_S"] = "10"
    os.environ["WATCHDOG_MIX_S"] = "10"
    os.environ["WATCHDOG_MUX_S"] = "10"
    os.environ["WATCHDOG_EXPORT_S"] = "10"

    from anime_v2.config import get_settings
    from anime_v2.jobs.models import Job, JobState
    from anime_v2.jobs.queue import JobQueue
    from anime_v2.jobs.store import JobStore

    get_settings.cache_clear()

    store = JobStore(out / "jobs.db")
    q = JobQueue(store, concurrency=1, app_root=Path("/workspace"))

    # Prepare import files
    imp_dir = (Path("/workspace/Input") / "imports" / "j_imp_1").resolve()
    imp_dir.mkdir(parents=True, exist_ok=True)
    src_srt = imp_dir / "src.srt"
    tgt_srt = imp_dir / "tgt.srt"
    src_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n\n", encoding="utf-8")
    tgt_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n\n", encoding="utf-8")

    job = Job(
        id="j_imp_1",
        owner_id="u1",
        video_path="/workspace/Input/Test.mp4",
        duration_s=1.0,
        mode="low",
        device="cpu",
        src_lang="ja",
        tgt_lang="en",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        state=JobState.QUEUED,
        progress=0.0,
        message="Queued",
        output_mkv="",
        output_srt="",
        work_dir="",
        log_path="",
        runtime={"imports": {"src_srt_path": str(src_srt), "tgt_srt_path": str(tgt_srt)}},
        error=None,
    )
    store.put(job)

    # Monkeypatch heavy stages so the run is fast and deterministic.
    import anime_v2.jobs.queue as qmod

    def _audio_extract_stub(*, video, out_dir, wav_out=None, **_kw):
        p = Path(wav_out or (Path(out_dir) / "audio.wav"))
        _write_silence_wav(p, seconds=1.0)
        return p

    def _transcribe_should_not_run(**_kw):
        raise RuntimeError("transcribe should be skipped when src_srt is imported")

    def _translate_should_not_run(*_a, **_kw):
        raise RuntimeError("translate should be skipped when tgt_srt is imported")

    def _tts_stub(*, out_dir, transcript_srt=None, translated_json=None, wav_out=None, **_kw):
        p = Path(wav_out or (Path(out_dir) / "tts.wav"))
        _write_silence_wav(p, seconds=1.0)
        return p

    def _mix_stub(*_a, **_kw):
        # Minimal outputs structure expected by queue
        out_dir = Path(_kw.get("out_dir"))
        mkv = out_dir / "stub.dub.mkv"
        mkv.write_bytes(b"mkv")
        return {"mkv": str(mkv), "mp4": None}

    def _mux_stub(*, out_mkv, **_kw):
        Path(out_mkv).parent.mkdir(parents=True, exist_ok=True)
        Path(out_mkv).write_bytes(b"mkv")

    def _export_stub(*, out_path=None, out_dir=None, **_kw):
        if out_path:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_bytes(b"mp4")
        if out_dir:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            (Path(out_dir) / "index.m3u8").write_text("#EXTM3U\n", encoding="utf-8")

    qmod.audio_extractor.extract = _audio_extract_stub  # type: ignore[attr-defined]
    qmod.transcribe = _transcribe_should_not_run  # type: ignore[assignment]
    qmod.translate_segments = _translate_should_not_run  # type: ignore[assignment]
    qmod.tts.run = _tts_stub  # type: ignore[attr-defined]
    qmod.mix = _mix_stub  # type: ignore[assignment]
    qmod.mkv_export.mux = _mux_stub  # type: ignore[attr-defined]
    # export_mobile_* are imported dynamically; patch module-level export functions via their module
    import anime_v2.stages.export as exmod

    exmod.export_mobile_mp4 = _export_stub  # type: ignore[assignment]
    exmod.export_mobile_hls = _export_stub  # type: ignore[assignment]

    asyncio.run(q._run_job(job.id))

    j2 = store.get(job.id)
    assert j2 is not None
    assert j2.state in {JobState.DONE, JobState.FAILED}, j2.state
    assert j2.state == JobState.DONE, (j2.state, j2.message, j2.error)
    skipped = (j2.runtime or {}).get("skipped_stages") if isinstance(j2.runtime, dict) else None
    assert isinstance(skipped, list) and any(s.get("stage") == "transcribe" for s in skipped)
    assert any(s.get("stage") == "translate" for s in skipped)

    print("verify_sub_import: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

