from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anime_v2.config import get_settings
from anime_v2.stages import audio_extractor, tts
from anime_v2.stages.transcription import transcribe
from anime_v2.stages.translation import TranslationConfig, translate_segments
from anime_v2.timing.pacing import pad_or_trim_wav
from anime_v2.utils.ffmpeg_safe import (
    FFmpegError,
    extract_audio_mono_16k,
    ffprobe_duration_seconds,
    run_ffmpeg,
)
from anime_v2.utils.io import atomic_write_text, write_json
from anime_v2.utils.log import logger
from anime_v2.utils.subtitles import write_srt, write_vtt


@dataclass(frozen=True, slots=True)
class RealtimeResult:
    out_dir: Path
    manifest_path: Path
    stitched_wav: Path | None
    stitched_srt: Path | None
    stitched_vtt: Path | None


def _concat_wavs_ffmpeg(wavs: list[Path], out_wav: Path) -> Path:
    """
    Concatenate WAVs using ffmpeg concat demuxer.
    """
    s = get_settings()
    out_wav.parent.mkdir(parents=True, exist_ok=True)

    def esc(p: Path) -> str:
        # ffmpeg concat demuxer uses: file '<path>'; escape single quotes.
        return p.as_posix().replace("'", r"'\''")

    lst = out_wav.with_suffix(".concat.txt")
    atomic_write_text(
        lst,
        "".join([f"file '{esc(w)}'\n" for w in wavs]),
        encoding="utf-8",
    )
    run_ffmpeg(
        [
            str(s.ffmpeg_bin),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(lst),
            "-c",
            "copy",
            str(out_wav),
        ],
        timeout_s=120,
        retries=0,
        capture=True,
    )
    return out_wav


def _pad_or_trim_to(wav_in: Path, *, duration_s: float, out_wav: Path) -> Path:
    """
    Ensure audio is exactly duration_s by trimming or padding silence.
    """
    return pad_or_trim_wav(wav_in, out_wav, float(duration_s), timeout_s=120)


def realtime_dub(
    *,
    video: Path,
    out_dir: Path,
    device: str,
    asr_model: str,
    src_lang: str,
    tgt_lang: str,
    mt_engine: str,
    mt_lowconf_thresh: float,
    glossary: str | None,
    style: str | None,
    chunk_seconds: float,
    chunk_overlap: float,
    stitch: bool,
    subs_choice: str,
    subs_format: str,
    align_mode: str,
    emotion_mode: str,
    expressive: str,
    expressive_strength: float,
    expressive_debug: bool,
    speech_rate: float,
    pitch: float,
    energy: float,
) -> RealtimeResult:
    """
    Pseudo-streaming (chunked) dubbing mode.

    Produces per-chunk artifacts under:
      Output/<stem>/realtime/
    And optionally stitches:
      - realtime.stitched.tts.wav
      - realtime.stitched.(srt|vtt)
    """
    video = Path(video)
    out_dir = Path(out_dir)
    rt_dir = out_dir / "realtime"
    rt_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()

    # 1) Extract full audio (mono 16k)
    wav_full = out_dir / "audio.wav"
    extracted = audio_extractor.extract(video=video, out_dir=out_dir, wav_out=wav_full)

    # 2) Determine duration
    try:
        total_dur = float(ffprobe_duration_seconds(extracted))
    except FFmpegError as ex:
        raise RuntimeError(f"realtime: failed to probe audio duration: {ex}") from ex
    if total_dur <= 0:
        raise RuntimeError("realtime: audio duration is zero")

    cs = max(2.0, float(chunk_seconds))
    ov = max(0.0, min(float(chunk_overlap), cs * 0.9))

    chunks: list[dict[str, Any]] = []
    stitched_segments: list[dict[str, Any]] = []
    chunk_wavs: list[Path] = []

    # 3) Chunk loop
    start = 0.0
    idx = 0
    while start < total_dur - 1e-3:
        idx += 1
        end = min(total_dur, start + cs)
        chunk_id = f"{idx:04d}"
        chunk_base = rt_dir / chunk_id
        chunk_base.mkdir(parents=True, exist_ok=True)

        wav_chunk = chunk_base / "chunk.wav"
        extract_audio_mono_16k(
            src=extracted, dst=wav_chunk, start_s=start, end_s=end, timeout_s=180
        )

        # 3a) ASR for chunk
        srt_chunk = chunk_base / "src.srt"
        transcribe(
            audio_path=wav_chunk,
            srt_out=srt_chunk,
            device=device,
            model_name=asr_model,
            task="transcribe",
            src_lang=src_lang,
            tgt_lang=tgt_lang,
            word_timestamps=(str(align_mode).lower() == "word"),
        )
        meta = json.loads(srt_chunk.with_suffix(".json").read_text(encoding="utf-8"))
        cues = meta.get("segments_detail", []) if isinstance(meta, dict) else []
        if not isinstance(cues, list) or not cues:
            cues = []

        segs_for_mt = []
        for c in cues:
            if not isinstance(c, dict):
                continue
            with_d = {
                "start": float(c.get("start", 0.0)),
                "end": float(c.get("end", 0.0)),
                "speaker": "SPEAKER_01",
                "text": str(c.get("text") or ""),
                "logprob": c.get("avg_logprob"),
            }
            segs_for_mt.append(with_d)

        # 3b) MT for chunk (best-effort)
        cfg = TranslationConfig(
            mt_engine=str(mt_engine).lower(),
            mt_lowconf_thresh=float(mt_lowconf_thresh),
            glossary_path=glossary,
            style_path=style,
            show_id=video.stem,
            whisper_model=asr_model,
            audio_path=str(wav_chunk),
            device=device,
        )
        translated = segs_for_mt
        try:
            if str(tgt_lang).lower() != "en" or str(mt_engine).lower() != "whisper":
                translated = translate_segments(
                    segs_for_mt, src_lang=src_lang, tgt_lang=tgt_lang, cfg=cfg
                )
        except Exception as ex:
            logger.warning("realtime: translate failed (chunk=%s): %s", chunk_id, ex)
            translated = segs_for_mt

        # 3c) Persist chunk transcripts
        tgt_lines = [
            {"start": s["start"], "end": s["end"], "text": s.get("text", "")} for s in translated
        ]
        if subs_choice in {"src", "both"}:
            write_srt(
                [
                    {"start": s["start"], "end": s["end"], "text": s.get("text", "")}
                    for s in segs_for_mt
                ],
                chunk_base / "src.srt",
            )
            if subs_format in {"vtt", "both"}:
                write_vtt(
                    [
                        {"start": s["start"], "end": s["end"], "text": s.get("text", "")}
                        for s in segs_for_mt
                    ],
                    chunk_base / "src.vtt",
                )
        if subs_choice in {"tgt", "both"}:
            write_srt(tgt_lines, chunk_base / "tgt.srt")
            if subs_format in {"vtt", "both"}:
                write_vtt(tgt_lines, chunk_base / "tgt.vtt")

        # 3d) TTS for chunk
        chunk_json = chunk_base / "translated.json"
        write_json(chunk_json, {"src_lang": src_lang, "tgt_lang": tgt_lang, "segments": translated})
        chunk_tts = chunk_base / "tts.wav"
        tts.run(
            out_dir=chunk_base,
            translated_json=chunk_json,
            diarization_json=None,
            wav_out=chunk_tts,
            tts_lang=tgt_lang,
            emotion_mode=emotion_mode,
            expressive=expressive,
            expressive_strength=float(expressive_strength),
            expressive_debug=bool(expressive_debug),
            source_audio_wav=wav_chunk,
            speech_rate=speech_rate,
            pitch=pitch,
            energy=energy,
            max_stretch=float(get_settings().max_stretch),
        )

        # Ensure audio spans the chunk duration (pad/trim)
        chunk_fixed = chunk_base / "tts.fixed.wav"
        _pad_or_trim_to(chunk_tts, duration_s=(end - start), out_wav=chunk_fixed)

        # Optional overlap trimming for stitch: drop the first ov seconds for all chunks after the first.
        if idx > 1 and ov > 0:
            trimmed = chunk_base / "tts.trim.wav"
            run_ffmpeg(
                [
                    str(get_settings().ffmpeg_bin),
                    "-y",
                    "-ss",
                    f"{ov:.3f}",
                    "-i",
                    str(chunk_fixed),
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(trimmed),
                ],
                timeout_s=120,
                retries=0,
                capture=True,
            )
            chunk_fixed = trimmed

        chunk_wavs.append(chunk_fixed)

        # Build stitched segment list (shift by absolute start; skip overlap prefix for chunk>1).
        for seg in translated:
            try:
                st = float(seg["start"])
                en = float(seg["end"])
                if idx > 1 and ov > 0 and en <= ov:
                    continue
                st2 = st - ov if idx > 1 else st
                en2 = en - ov if idx > 1 else en
                stitched_segments.append(
                    {
                        "start": float(start) + max(0.0, st2),
                        "end": float(start) + max(0.0, en2),
                        "text": str(seg.get("text") or ""),
                    }
                )
            except Exception:
                continue

        chunks.append(
            {
                "id": chunk_id,
                "start_s": start,
                "end_s": end,
                "wav": str(wav_chunk),
                "tts_wav": str(chunk_fixed),
            }
        )

        # Advance start with overlap
        if end >= total_dur:
            break
        start = max(0.0, end - ov)

    manifest_path = rt_dir / "manifest.json"
    manifest = {
        "version": 1,
        "video": str(video),
        "audio": str(extracted),
        "chunk_seconds": cs,
        "chunk_overlap": ov,
        "chunks": chunks,
        "stitched_segments": len(stitched_segments),
        "wall_time_s": time.perf_counter() - t0,
    }
    atomic_write_text(
        manifest_path,
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    stitched_wav = None
    stitched_srt = None
    stitched_vtt = None

    if stitch and chunk_wavs:
        stitched_wav = rt_dir / "realtime.stitched.tts.wav"
        _concat_wavs_ffmpeg(chunk_wavs, stitched_wav)
        if subs_choice in {"tgt", "both"}:
            stitched_srt = rt_dir / "realtime.stitched.srt"
            write_srt(stitched_segments, stitched_srt)
            if subs_format in {"vtt", "both"}:
                stitched_vtt = rt_dir / "realtime.stitched.vtt"
                write_vtt(stitched_segments, stitched_vtt)

    logger.info(
        "realtime_done",
        out_dir=str(rt_dir),
        chunks=len(chunks),
        wall_time_s=manifest["wall_time_s"],
    )
    return RealtimeResult(
        out_dir=rt_dir,
        manifest_path=manifest_path,
        stitched_wav=stitched_wav,
        stitched_srt=stitched_srt,
        stitched_vtt=stitched_vtt,
    )
