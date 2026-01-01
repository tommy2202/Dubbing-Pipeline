from __future__ import annotations

import time
from pathlib import Path

import click

from anime_v2.stages import audio_extractor, mkv_export, tts
from anime_v2.stages import diarize
from anime_v2.stages.translate import translate_lines
from anime_v2.stages.transcription import transcribe
from anime_v2.utils.io import read_json, write_json
from anime_v2.utils.log import logger
from anime_v2.utils.paths import output_dir_for
from anime_v2.utils.time import format_srt_timestamp


MODE_TO_MODEL: dict[str, str] = {
    "high": "large-v3",
    "medium": "medium",
    "low": "small",
}


def _select_device(device: str) -> str:
    device = device.lower()
    if device in {"cpu", "cuda"}:
        return device
    if device != "auto":
        return "cpu"

    try:
        import torch  # type: ignore

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _parse_srt_to_cues(srt_path: Path) -> list[dict]:
    """
    Parse SRT into cues: [{start,end,text}]
    """
    if not srt_path.exists():
        return []
    text = srt_path.read_text(encoding="utf-8", errors="replace")
    blocks = [b for b in text.split("\n\n") if b.strip()]
    cues: list[dict] = []

    def parse_ts(ts: str) -> float:
        hh, mm, rest = ts.split(":")
        ss, ms = rest.split(",")
        return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0

    for b in blocks:
        lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        start_s, end_s = [p.strip() for p in lines[1].split("-->", 1)]
        start = parse_ts(start_s)
        end = parse_ts(end_s)
        cue_text = " ".join(lines[2:]).strip() if len(lines) > 2 else ""
        cues.append({"start": start, "end": end, "text": cue_text})
    return cues


def _assign_speakers(cues: list[dict], diar_segments: list[dict] | None) -> list[dict]:
    diar_segments = diar_segments or []
    out: list[dict] = []
    for c in cues:
        start = float(c["start"])
        end = float(c["end"])
        mid = (start + end) / 2.0
        speaker_id = "Speaker1"
        for seg in diar_segments:
            try:
                if float(seg["start"]) <= mid <= float(seg["end"]):
                    speaker_id = str(seg.get("speaker_id") or speaker_id)
                    break
            except Exception:
                continue
        out.append({"start": start, "end": end, "speaker_id": speaker_id, "text": str(c.get("text", "") or "")})
    return out


def _write_srt_from_lines(lines: list[dict], srt_path: Path) -> None:
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    with srt_path.open("w", encoding="utf-8") as f:
        for idx, line in enumerate(lines, 1):
            st = format_srt_timestamp(float(line["start"]))
            en = format_srt_timestamp(float(line["end"]))
            txt = str(line.get("text", "") or "").strip()
            f.write(f"{idx}\n{st} --> {en}\n{txt}\n\n")


@click.command()
@click.argument("video", type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--device",
    type=click.Choice(["auto", "cuda", "cpu"], case_sensitive=False),
    default="auto",
    show_default=True,
)
@click.option(
    "--mode",
    type=click.Choice(["high", "medium", "low"], case_sensitive=False),
    default="medium",
    show_default=True,
)
@click.option("--src-lang", default="auto", show_default=True, help="Source language code, or 'auto'")
@click.option("--tgt-lang", default="en", show_default=True, help="Target language code (translate pathway outputs English)")
@click.option("--no-translate", is_flag=True, default=False, help="Disable translate; do plain transcription")
@click.option("--no-subs", is_flag=True, default=False, help="Do not mux subtitles into output container")
def cli(video: Path, device: str, mode: str, src_lang: str, tgt_lang: str, no_translate: bool, no_subs: bool) -> None:
    """
    Run pipeline-v2 on VIDEO.

    Example:
      anime-v2 Input/Test.mp4 --mode high --device auto
    """
    if not video.exists():
        raise click.ClickException(f"Video not found: {video}")

    mode = mode.lower()
    chosen_model = MODE_TO_MODEL[mode]
    chosen_device = _select_device(device)

    # Whisper should only do ASR; translation is handled by stages.translate to support any src→tgt.
    task = "transcribe"

    # Output layout requirement:
    # Output/<video_stem>/{wav,srt,tts.wav,dub.mkv}
    stem = video.stem
    out_dir = output_dir_for(video)
    out_dir.mkdir(parents=True, exist_ok=True)

    wav_path = out_dir / "audio.wav"
    srt_out = out_dir / f"{stem}.srt"
    translated_srt = out_dir / f"{stem}.translated.srt"
    diar_json = out_dir / "diarization.json"
    translated_json = out_dir / "translated.json"
    tts_wav = out_dir / f"{stem}.tts.wav"
    dub_mkv = out_dir / "dub.mkv"

    logger.info("[v2] Starting dub: video=%s mode=%s model=%s device=%s", video, mode, chosen_model, chosen_device)
    logger.info("[v2] Languages: src_lang=%s tgt_lang=%s translate=%s", src_lang, tgt_lang, (not no_translate))

    t0 = time.perf_counter()

    # 1) audio_extractor.extract
    extracted: Path | None = None
    t_stage = time.perf_counter()
    try:
        extracted = audio_extractor.extract(video=video, out_dir=out_dir, wav_out=wav_path)
        logger.info("[v2] audio_extractor: ok path=%s (%.2fs)", extracted, time.perf_counter() - t_stage)
    except Exception as ex:
        logger.exception("[v2] audio_extractor failed: %s", ex)
        logger.error("[v2] Done. Output: (failed early)")
        return

    # 2) diarize.identify
    diar_segments: list[dict] = []
    speaker_embeddings: dict[str, str] = {}
    try:
        t_stage = time.perf_counter()
        diar_segments, speaker_embeddings = diarize.identify(audio_path=extracted, out_dir=out_dir)
        write_json(
            diar_json,
            {
                "audio_path": str(extracted),
                "segments": diar_segments,
                "speaker_embeddings": speaker_embeddings,
            },
        )
        logger.info("[v2] diarize: diar_segments=%s stable_speakers=%s (%.2fs)", len(diar_segments), len(set(s.get("speaker_id") for s in diar_segments)), time.perf_counter() - t_stage)
    except Exception as ex:
        logger.exception("[v2] diarize failed (continuing): %s", ex)

    # 3) transcription.transcribe (ASR-only)
    t_stage = time.perf_counter()
    try:
        transcribe(
            audio_path=extracted,
            srt_out=srt_out,
            device=chosen_device,
            model_name=chosen_model,
            task="transcribe",
            src_lang=src_lang,
            tgt_lang=tgt_lang,
        )
        cues = _parse_srt_to_cues(srt_out)
        logger.info("[v2] transcription: cues=%s (%.2fs) → %s", len(cues), time.perf_counter() - t_stage, srt_out)
    except Exception as ex:
        logger.exception("[v2] transcription failed (continuing): %s", ex)
        cues = []

    # Assign speakers from diarization (if any)
    lines = _assign_speakers(cues, diar_segments)

    # 4) translate.translate_lines (when needed)
    subs_srt_path: Path | None = srt_out
    if no_translate:
        logger.info("[v2] translate: disabled (--no-translate)")
        try:
            write_json(translated_json, {"src_lang": src_lang, "tgt_lang": tgt_lang, "lines": lines})
        except Exception:
            pass
    else:
        t_stage = time.perf_counter()
        try:
            translated_lines = translate_lines(lines, src_lang=src_lang, tgt_lang=tgt_lang)
            # Ensure per-line fallback to original text if translation produced empties
            safe_lines = []
            for orig, tr in zip(lines, translated_lines):
                tr_text = str(tr.get("text", "") or "").strip()
                if str(orig.get("text", "") or "").strip() and not tr_text:
                    tr = dict(tr)
                    tr["text"] = orig["text"]
                safe_lines.append(tr)
            write_json(translated_json, {"src_lang": src_lang, "tgt_lang": tgt_lang, "lines": safe_lines})
            _write_srt_from_lines(safe_lines, translated_srt)
            subs_srt_path = translated_srt
            logger.info("[v2] translate: lines=%s (%.2fs) → %s", len(safe_lines), time.perf_counter() - t_stage, translated_json)
            lines = safe_lines
        except Exception as ex:
            logger.exception("[v2] translate failed (continuing with original text): %s", ex)
            try:
                write_json(translated_json, {"src_lang": src_lang, "tgt_lang": tgt_lang, "lines": lines, "error": str(ex)})
            except Exception:
                pass

    # 5) tts.synthesize (line-aligned)
    t_stage = time.perf_counter()
    try:
        tts.run(out_dir=out_dir, translated_json=translated_json, diarization_json=diar_json, wav_out=tts_wav)
        logger.info("[v2] tts: ok (%.2fs) → %s", time.perf_counter() - t_stage, tts_wav)
    except Exception as ex:
        logger.exception("[v2] tts failed (continuing with silence track): %s", ex)
        # Best-effort: create a silence wav of full duration using cue ends
        try:
            from anime_v2.stages.tts import _write_silence_wav  # type: ignore

            dur = max((float(l["end"]) for l in lines), default=0.0)
            _write_silence_wav(tts_wav, duration_s=dur)
        except Exception:
            pass

    # 6) mkv_export.mux
    t_stage = time.perf_counter()
    try:
        mkv_export.run(
            video=video,
            dubbed_audio=tts_wav,
            srt_path=None if no_subs else subs_srt_path,
            mkv_out=dub_mkv,
            ckpt_dir=out_dir,
            out_dir=out_dir,
        )
        logger.info("[v2] mux: ok (%.2fs) → %s", time.perf_counter() - t_stage, dub_mkv)
    except Exception as ex:
        logger.exception("[v2] mux failed: %s", ex)
        logger.error("[v2] Done. Output: (mux failed)")
        return

    logger.info("[v2] Done in %.2fs", time.perf_counter() - t0)
    logger.info("[v2] Done. Output: %s", dub_mkv)


if __name__ == "__main__":  # pragma: no cover
    cli()
