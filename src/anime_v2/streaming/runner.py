from __future__ import annotations

import json
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from anime_v2.config import get_settings
from anime_v2.stages.audio_extractor import extract as extract_audio
from anime_v2.stages.transcription import transcribe
from anime_v2.stages.translation import TranslationConfig, translate_segments
from anime_v2.stages.tts import _write_silence_wav
from anime_v2.streaming.chunker import Chunk, split_audio_to_chunks
from anime_v2.timing.pacing import pad_or_trim_wav
from anime_v2.utils.ffmpeg_safe import run_ffmpeg
from anime_v2.utils.io import atomic_write_text, read_json, write_json
from anime_v2.utils.log import logger


@dataclass(frozen=True, slots=True)
class StreamChunkResult:
    idx: int
    start_s: float
    end_s: float
    wav_chunk: Path
    src_srt: Path | None
    tgt_srt: Path | None
    translated_json: Path | None
    tts_wav: Path | None
    dubbed_wav: Path | None
    chunk_mp4: Path | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("wav_chunk", "src_srt", "tgt_srt", "translated_json", "tts_wav", "dubbed_wav", "chunk_mp4"):
            if d.get(k) is not None:
                d[k] = str(d[k])
        return d


def _concat_mp4s_ffmpeg(mp4s: list[Path], out_mp4: Path) -> Path:
    """
    Concatenate MP4 files using ffmpeg concat demuxer (requires compatible encodes).
    """
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    def esc(p: Path) -> str:
        return p.as_posix().replace("'", r"'\''")

    lst = out_mp4.with_suffix(".concat.txt")
    atomic_write_text(lst, "".join([f"file '{esc(p)}'\n" for p in mp4s]), encoding="utf-8")
    run_ffmpeg(
        [
            str(get_settings().ffmpeg_bin),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(lst),
            "-c",
            "copy",
            str(out_mp4),
        ],
        timeout_s=600,
        retries=0,
        capture=True,
    )
    return out_mp4


def _slice_video_segment(video: Path, *, start_s: float, end_s: float, out_mp4: Path) -> Path:
    """
    Re-encode a video segment to a consistent H.264 baseline profile so concat works reliably.
    """
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(get_settings().ffmpeg_bin),
        "-y",
        "-ss",
        f"{float(start_s):.3f}",
        "-to",
        f"{float(end_s):.3f}",
        "-i",
        str(video),
        "-an",
        "-c:v",
        "libx264",
        "-profile:v",
        "baseline",
        "-level",
        "3.0",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "veryfast",
        "-crf",
        "22",
        "-movflags",
        "+faststart",
        str(out_mp4),
    ]
    run_ffmpeg(cmd, timeout_s=600, retries=0, capture=True)
    return out_mp4


def _mux_chunk(video_seg: Path, *, dubbed_wav: Path, out_mp4: Path) -> Path:
    """
    Mux re-encoded video segment + dubbed wav into MP4.
    """
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(get_settings().ffmpeg_bin),
        "-y",
        "-i",
        str(video_seg),
        "-i",
        str(dubbed_wav),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(out_mp4),
    ]
    run_ffmpeg(cmd, timeout_s=600, retries=0, capture=True)
    return out_mp4


def run_streaming(
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
    stream: bool,
    chunk_seconds: float,
    overlap_seconds: float,
    stream_output: str,  # segments|final
    stream_concurrency: int = 1,
    timing_fit: bool = False,
    pacing: bool = False,
    pacing_min_ratio: float = 0.88,
    pacing_max_ratio: float = 1.18,
    timing_tolerance: float = 0.10,
    align_mode: str = "stretch",
    emotion_mode: str = "off",
    expressive: str = "off",
    expressive_strength: float = 0.5,
    expressive_debug: bool = False,
    speech_rate: float = 1.0,
    pitch: float = 1.0,
    energy: float = 1.0,
    music_detect: bool = False,
    music_mode: str = "auto",
    music_threshold: float = 0.70,
    op_ed_detect: bool = False,
    op_ed_seconds: int = 90,
    pg: str = "off",
    pg_policy_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Tier-3C pseudo-streaming:
    - Extract full audio
    - Chunk into overlapping windows under Output/<job>/chunks/
    - For each chunk: ASR -> MT -> (optional timing fit) -> TTS (with pacing controls)
    - Create chunk MP4 segments under Output/<job>/stream/
    - Write Output/<job>/stream/manifest.json
    - Optional stitch to Output/<job>/stream/stream.final.mp4
    """
    if not stream:
        raise ValueError("run_streaming called with stream=False")

    video = Path(video)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    s = get_settings()
    t0 = time.perf_counter()

    chunks_dir = out_dir / "chunks"
    stream_dir = out_dir / "stream"
    stream_dir.mkdir(parents=True, exist_ok=True)

    # 1) Extract full audio (mono16k)
    wav_full = out_dir / "audio.wav"
    extracted = extract_audio(video=video, out_dir=out_dir, wav_out=wav_full)

    # Tier-Next A/B: optional job-level music region detection (used to suppress dubbing + preserve original)
    full_music_regions: list[dict[str, Any]] = []
    if bool(music_detect):
        try:
            from anime_v2.audio.music_detect import (
                analyze_audio_for_music_regions,
                detect_op_ed,
                write_oped_json,
                write_regions_json,
            )

            analysis_dir = out_dir / "analysis"
            analysis_dir.mkdir(parents=True, exist_ok=True)
            regs = analyze_audio_for_music_regions(
                extracted,
                mode=str(music_mode).lower(),
                threshold=float(music_threshold),
            )
            write_regions_json(regs, analysis_dir / "music_regions.json")
            full_music_regions = [r.to_dict() for r in regs]
            logger.info(
                "stream_music_detect_regions",
                regions=len(full_music_regions),
                threshold=float(music_threshold),
                mode=str(music_mode).lower(),
            )
            if bool(op_ed_detect):
                oped = detect_op_ed(
                    extracted,
                    music_regions=regs,
                    seconds=int(op_ed_seconds),
                    threshold=float(music_threshold),
                )
                write_oped_json(oped, analysis_dir / "op_ed.json")
        except Exception:
            logger.exception("stream_music_detect_failed_continue")
            full_music_regions = []

    # 2) Build chunk wavs
    chunks: list[Chunk] = split_audio_to_chunks(
        source_wav=extracted,
        out_dir=chunks_dir,
        chunk_seconds=float(chunk_seconds),
        overlap_seconds=float(overlap_seconds),
    )
    if not chunks:
        raise RuntimeError("stream: no chunks produced (audio duration is zero?)")

    results: list[StreamChunkResult] = []
    mp4s: list[Path] = []
    pg_reports: list[dict[str, Any]] = []

    # NOTE: keep concurrency at 1 by default; higher values are best-effort and may be memory-heavy.
    if stream_concurrency and int(stream_concurrency) > 1:
        logger.warning("stream: concurrency>1 is best-effort; running sequentially for safety")

    for ch in chunks:
        chunk_id = f"{ch.idx:03d}"
        chunk_base = stream_dir / f"chunk_{chunk_id}"
        chunk_base.mkdir(parents=True, exist_ok=True)

        src_srt = chunk_base / "src.srt"
        tgt_srt = chunk_base / "tgt.srt"
        translated_json = chunk_base / "translated.json"
        tts_wav = chunk_base / "tts.wav"
        dubbed_wav = chunk_base / "dubbed.wav"
        chunk_mp4 = stream_dir / f"chunk_{chunk_id}.mp4"

        try:
            if dry_run:
                # Minimal artifacts; skip ASR/MT/TTS and create silence audio over video segment.
                _write_silence_wav(dubbed_wav, duration_s=max(0.05, ch.end_s - ch.start_s))
            else:
                # 3a) ASR
                transcribe(
                    audio_path=ch.wav_path,
                    srt_out=src_srt,
                    device=device,
                    model_name=asr_model,
                    task="transcribe",
                    src_lang=src_lang,
                    tgt_lang=tgt_lang,
                    word_timestamps=(str(align_mode).lower() == "word"),
                )
                meta = read_json(src_srt.with_suffix(".json"), default={})
                cues = meta.get("segments_detail", []) if isinstance(meta, dict) else []
                if not isinstance(cues, list):
                    cues = []
                segs_for_mt = []
                for c in cues:
                    if not isinstance(c, dict):
                        continue
                    segs_for_mt.append(
                        {
                            "start": float(c.get("start", 0.0)),
                            "end": float(c.get("end", 0.0)),
                            "speaker": "SPEAKER_01",
                            "text": str(c.get("text") or ""),
                            "logprob": c.get("avg_logprob"),
                        }
                    )

                # 3b) MT
                cfg = TranslationConfig(
                    mt_engine=str(mt_engine).lower(),
                    mt_lowconf_thresh=float(mt_lowconf_thresh),
                    glossary_path=glossary,
                    style_path=style,
                    show_id=video.stem,
                    whisper_model=asr_model,
                    audio_path=str(ch.wav_path),
                    device=device,
                )
                translated = segs_for_mt
                if str(tgt_lang).lower() != "en" or str(mt_engine).lower() != "whisper":
                    with suppress(Exception):
                        translated = translate_segments(
                            segs_for_mt, src_lang=src_lang, tgt_lang=tgt_lang, cfg=cfg
                        )

                # Tier-Next C: per-run PG mode (opt-in; OFF by default), before timing-fit/TTS/subs.
                if str(pg).lower() != "off":
                    try:
                        from anime_v2.text.pg_filter import apply_pg_filter_to_segments

                        translated, rep = apply_pg_filter_to_segments(
                            translated,
                            pg=str(pg).lower(),
                            pg_policy_path=Path(pg_policy_path).resolve() if pg_policy_path else None,
                            report_path=None,
                            job_id=str(out_dir.name),
                        )
                        pg_reports.append({"chunk_idx": int(ch.idx), "report": rep})
                    except Exception:
                        logger.exception("stream_pg_filter_failed_continue", idx=ch.idx)

                # 3c) Optional timing-fit (Tier-1B) inside the chunk.
                if timing_fit:
                    with suppress(Exception):
                        from anime_v2.timing.fit_text import fit_translation_to_time

                        for seg in translated:
                            try:
                                tgt_s = max(0.0, float(seg["end"]) - float(seg["start"]))
                                pre = str(seg.get("text") or "")
                                fitted, stats = fit_translation_to_time(
                                    pre,
                                    tgt_s,
                                    tolerance=float(timing_tolerance),
                                    wps=float(getattr(s, "timing_wps", 2.7)),
                                    max_passes=4,
                                )
                                seg["text_pre_fit"] = pre
                                seg["text"] = fitted
                                seg["timing_fit"] = stats.to_dict()
                            except Exception:
                                continue

                write_json(translated_json, {"src_lang": src_lang, "tgt_lang": tgt_lang, "segments": translated})

                # Simple target SRT for the chunk (best-effort)
                with suppress(Exception):
                    from anime_v2.utils.subtitles import write_srt

                    write_srt(
                        [{"start": s["start"], "end": s["end"], "text": s.get("text", "")} for s in translated],
                        tgt_srt,
                    )

                # 3d) TTS
                from anime_v2.stages import tts as tts_stage

                # Chunk-local regions (relative to chunk timeline) for suppressing dubbing.
                music_regions_path = None
                chunk_music_regions: list[dict[str, Any]] = []
                if full_music_regions:
                    for r in full_music_regions:
                        try:
                            rs = float(r.get("start", 0.0))
                            re = float(r.get("end", 0.0))
                        except Exception:
                            continue
                        if re <= float(ch.start_s) or rs >= float(ch.end_s):
                            continue
                        cs = max(float(ch.start_s), rs) - float(ch.start_s)
                        ce = min(float(ch.end_s), re) - float(ch.start_s)
                        if ce > cs:
                            chunk_music_regions.append(
                                {
                                    "start": float(cs),
                                    "end": float(ce),
                                    "kind": str(r.get("kind") or "music"),
                                    "confidence": float(r.get("confidence", 1.0)),
                                    "reason": str(r.get("reason") or ""),
                                }
                            )
                if chunk_music_regions:
                    from anime_v2.audio.music_detect import Region, write_regions_json

                    analysis_dir = chunk_base / "analysis"
                    analysis_dir.mkdir(parents=True, exist_ok=True)
                    music_regions_path = analysis_dir / "music_regions.json"
                    write_regions_json(
                        [
                            Region(
                                start=float(rr["start"]),
                                end=float(rr["end"]),
                                kind=str(rr.get("kind") or "music"),
                                confidence=float(rr.get("confidence", 1.0)),
                                reason=str(rr.get("reason") or ""),
                            )
                            for rr in chunk_music_regions
                        ],
                        music_regions_path,
                    )

                tts_stage.run(
                    out_dir=chunk_base,
                    translated_json=translated_json,
                    diarization_json=None,
                    wav_out=tts_wav,
                    tts_lang=tgt_lang,
                    emotion_mode=str(emotion_mode),
                    expressive=str(expressive),
                    expressive_strength=float(expressive_strength),
                    expressive_debug=bool(expressive_debug),
                    source_audio_wav=ch.wav_path,
                    music_regions_path=music_regions_path,
                    speech_rate=float(speech_rate),
                    pitch=float(pitch),
                    energy=float(energy),
                    pacing=bool(pacing),
                    pacing_min_ratio=float(pacing_min_ratio),
                    pacing_max_ratio=float(pacing_max_ratio),
                    timing_tolerance=float(timing_tolerance),
                    timing_debug=False,
                    max_stretch=float(getattr(s, "max_stretch", 0.15)),
                )

                # Ensure chunk audio spans the chunk duration
                tts_full = chunk_base / "tts.full.wav"
                pad_or_trim_wav(tts_wav, tts_full, float(ch.end_s - ch.start_s), timeout_s=120)

                # If chunk overlaps music regions, preserve original chunk audio in those intervals.
                if chunk_music_regions:
                    parts = []
                    for rr in chunk_music_regions:
                        a = max(0.0, float(rr.get("start", 0.0)))
                        b = max(a, float(rr.get("end", 0.0)))
                        parts.append(f"between(t,{a:.3f},{b:.3f})")
                    cond = "+".join(parts) if parts else "0"
                    music_only = chunk_base / "music_only.wav"
                    run_ffmpeg(
                        [
                            str(s.ffmpeg_bin),
                            "-y",
                            "-i",
                            str(ch.wav_path),
                            "-filter:a",
                            f"volume='if({cond},1,0)':eval=frame",
                            "-ac",
                            "1",
                            "-ar",
                            "16000",
                            str(music_only),
                        ],
                        timeout_s=120,
                        retries=0,
                        capture=True,
                    )
                    run_ffmpeg(
                        [
                            str(s.ffmpeg_bin),
                            "-y",
                            "-i",
                            str(tts_full),
                            "-i",
                            str(music_only),
                            "-filter_complex",
                            "[0:a][1:a]amix=inputs=2:normalize=0",
                            "-ac",
                            "1",
                            "-ar",
                            "16000",
                            str(dubbed_wav),
                        ],
                        timeout_s=120,
                        retries=0,
                        capture=True,
                    )
                else:
                    dubbed_wav.write_bytes(tts_full.read_bytes())

            # 4) Chunk MP4: slice video segment and mux dubbed audio
            video_seg = chunk_base / "video.mp4"
            _slice_video_segment(video, start_s=ch.start_s, end_s=ch.end_s, out_mp4=video_seg)
            _mux_chunk(video_seg, dubbed_wav=dubbed_wav, out_mp4=chunk_mp4)
            mp4s.append(chunk_mp4)

            results.append(
                StreamChunkResult(
                    idx=ch.idx,
                    start_s=ch.start_s,
                    end_s=ch.end_s,
                    wav_chunk=ch.wav_path,
                    src_srt=src_srt if src_srt.exists() else None,
                    tgt_srt=tgt_srt if tgt_srt.exists() else None,
                    translated_json=translated_json if translated_json.exists() else None,
                    tts_wav=tts_wav if tts_wav.exists() else None,
                    dubbed_wav=dubbed_wav if dubbed_wav.exists() else None,
                    chunk_mp4=chunk_mp4 if chunk_mp4.exists() else None,
                    error=None,
                )
            )
        except Exception as ex:
            logger.warning("stream_chunk_failed", idx=ch.idx, error=str(ex))
            results.append(
                StreamChunkResult(
                    idx=ch.idx,
                    start_s=ch.start_s,
                    end_s=ch.end_s,
                    wav_chunk=ch.wav_path,
                    src_srt=None,
                    tgt_srt=None,
                    translated_json=None,
                    tts_wav=None,
                    dubbed_wav=None,
                    chunk_mp4=None,
                    error=str(ex),
                )
            )

    manifest_path = stream_dir / "manifest.json"
    manifest: dict[str, Any] = {
        "version": 1,
        "video": str(video),
        "audio": str(extracted),
        "chunk_seconds": float(chunk_seconds),
        "overlap_seconds": float(overlap_seconds),
        "chunks_dir": str(chunks_dir),
        "stream_dir": str(stream_dir),
        "chunks": [r.to_dict() for r in results],
        "stream_output": str(stream_output),
        "wall_time_s": time.perf_counter() - t0,
    }
    atomic_write_text(manifest_path, json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    # Per-job PG filter report (best-effort) for streaming runs.
    if str(pg).lower() != "off":
        with suppress(Exception):
            analysis_dir = out_dir / "analysis"
            analysis_dir.mkdir(parents=True, exist_ok=True)
            from anime_v2.utils.io import write_json as _wj

            _wj(analysis_dir / "pg_filter_report.json", {"version": 1, "chunks": pg_reports})

    final_out = None
    if str(stream_output).lower() == "final" and mp4s:
        final_out = stream_dir / "stream.final.mp4"
        _concat_mp4s_ffmpeg(mp4s, final_out)

    return {"manifest": manifest_path, "final": final_out, "chunks": mp4s}

