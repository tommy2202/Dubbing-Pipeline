from __future__ import annotations

import time
from pathlib import Path

import click

from anime_v2.stages import audio_extractor, mkv_export, tts
from anime_v2.stages.align import AlignConfig, realign_srt
from anime_v2.stages.character_store import CharacterStore
from anime_v2.stages.diarization import DiarizeConfig, diarize as diarize_v2
from anime_v2.stages.translation import TranslationConfig, translate_segments
from anime_v2.stages.transcription import transcribe
from anime_v2.utils.io import read_json, write_json
from anime_v2.utils.log import logger
from anime_v2.utils.paths import output_dir_for
from anime_v2.utils.time import format_srt_timestamp
import subprocess

from anime_v2.utils.embeds import ecapa_embedding


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
@click.option("--diarizer", type=click.Choice(["auto", "pyannote", "speechbrain", "heuristic"], case_sensitive=False), default="auto", show_default=True)
@click.option("--show-id", default=None, help="Show id used for persistent character IDs (default: input basename)")
@click.option("--char-sim-thresh", type=float, default=0.72, show_default=True)
@click.option("--mt-engine", type=click.Choice(["auto", "whisper", "marian", "nllb"], case_sensitive=False), default="auto", show_default=True)
@click.option("--mt-lowconf-thresh", type=float, default=-0.45, show_default=True, help="Avg logprob threshold for Whisper translate fallback")
@click.option("--glossary", default=None, help="Glossary TSV path or directory")
@click.option("--style", default=None, help="Style YAML path or directory")
@click.option("--aligner", type=click.Choice(["auto", "aeneas", "heuristic"], case_sensitive=False), default="auto", show_default=True)
@click.option("--max-stretch", type=float, default=0.15, show_default=True, help="Max +/- time-stretch applied to TTS clips")
def cli(
    video: Path,
    device: str,
    mode: str,
    src_lang: str,
    tgt_lang: str,
    no_translate: bool,
    no_subs: bool,
    diarizer: str,
    show_id: str | None,
    char_sim_thresh: float,
    mt_engine: str,
    mt_lowconf_thresh: float,
    glossary: str | None,
    style: str | None,
    aligner: str,
    max_stretch: float,
) -> None:
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

    # 2) diarization + persistent character IDs
    diar_segments: list[dict] = []
    speaker_embeddings: dict[str, str] = {}
    try:
        t_stage = time.perf_counter()
        show = show_id or video.stem
        cfg = DiarizeConfig(diarizer=diarizer.lower())
        utts = diarize_v2(str(extracted), device=chosen_device, cfg=cfg)

        # Extract per-utterance wavs and compute per-speaker embeddings for re-ID
        seg_dir = out_dir / "segments"
        seg_dir.mkdir(parents=True, exist_ok=True)
        by_label: dict[str, list[tuple[float, float, Path]]] = {}
        for i, u in enumerate(utts):
            s = float(u["start"])
            e = float(u["end"])
            lab = str(u["speaker"])
            seg_wav = seg_dir / f"{i:04d}_{lab}.wav"
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", f"{s:.3f}", "-to", f"{e:.3f}", "-i", str(extracted), "-ac", "1", "-ar", "16000", str(seg_wav)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                seg_wav = Path(str(extracted))
            by_label.setdefault(lab, []).append((s, e, seg_wav))

        store = CharacterStore.default()
        store.load()
        thresholds = {"sim": float(char_sim_thresh)}
        # Map diar speaker label -> persistent character id
        lab_to_char: dict[str, str] = {}
        for lab, segs in by_label.items():
            # pick longest seg wav for embedding
            segs_sorted = sorted(segs, key=lambda t: (t[1] - t[0]), reverse=True)
            rep_wav = segs_sorted[0][2]
            emb = ecapa_embedding(rep_wav, device=chosen_device)
            if emb is None:
                # no embeddings => stable ID per show label
                lab_to_char[lab] = lab
                continue
            cid = store.match_or_create(emb, show_id=show, thresholds=thresholds)
            store.link_speaker_wav(cid, str(rep_wav))
            lab_to_char[lab] = cid
            # also persist npy for downstream preset matching (optional)
            try:
                import numpy as np  # type: ignore

                emb_dir = Path("voices") / "embeddings"
                emb_dir.mkdir(parents=True, exist_ok=True)
                emb_path = emb_dir / f"{cid}.npy"
                np.save(str(emb_path), emb.astype("float32"))
                speaker_embeddings[cid] = str(emb_path)
            except Exception:
                pass

        store.save()

        diar_segments = []
        for lab, segs in by_label.items():
            for s, e, wav_p in segs:
                diar_segments.append(
                    {
                        "start": s,
                        "end": e,
                        "diar_label": lab,
                        "speaker_id": lab_to_char.get(lab, lab),
                        "wav_path": str(wav_p),
                        "conf": float(next((u["conf"] for u in utts if str(u["speaker"]) == lab), 0.0)),
                    }
                )

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
    trans_meta_path = srt_out.with_suffix(".json")
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
        meta = {}
        try:
            meta = read_json(trans_meta_path, default={})  # type: ignore[assignment]
        except Exception:
            meta = {}
        segs_detail = meta.get("segments_detail", []) if isinstance(meta, dict) else []
        cues = segs_detail if isinstance(segs_detail, list) else []
        logger.info("[v2] transcription: segments=%s (%.2fs) → %s", len(cues), time.perf_counter() - t_stage, srt_out)
    except Exception as ex:
        logger.exception("[v2] transcription failed (continuing): %s", ex)
        cues = []

    # Build speaker-timed segments.
    # Prefer diarization utterances (preserve diarization timing) and assign text/logprob from transcription overlaps.
    diar_utts = sorted(
        [{"start": float(s["start"]), "end": float(s["end"]), "speaker": str(s.get("speaker_id") or "SPEAKER_01")} for s in diar_segments],
        key=lambda x: (x["start"], x["end"]),
    )

    def overlap(a0, a1, b0, b1) -> float:
        return max(0.0, min(a1, b1) - max(a0, b0))

    segments_for_mt: list[dict] = []
    if diar_utts:
        for u in diar_utts:
            txt_parts = []
            lp_parts = []
            w_parts = []
            for seg in cues:
                try:
                    s0 = float(seg["start"])
                    s1 = float(seg["end"])
                    ov = overlap(u["start"], u["end"], s0, s1)
                    if ov <= 0:
                        continue
                    t = str(seg.get("text") or "").strip()
                    if t:
                        txt_parts.append(t)
                    lp = seg.get("avg_logprob")
                    if lp is not None:
                        try:
                            lp_parts.append(float(lp))
                            w_parts.append(ov)
                        except Exception:
                            pass
                except Exception:
                    continue
            text_src = " ".join(txt_parts).strip()
            logprob = None
            if lp_parts and w_parts and len(lp_parts) == len(w_parts):
                tot = sum(w_parts)
                if tot > 0:
                    logprob = sum(lp * w for lp, w in zip(lp_parts, w_parts)) / tot
            segments_for_mt.append({"start": u["start"], "end": u["end"], "speaker": u["speaker"], "text": text_src, "logprob": logprob})
    else:
        # Fall back to transcription segments (speaker assignment best-effort)
        segments_for_mt = []
        for seg in cues:
            try:
                segments_for_mt.append(
                    {
                        "start": float(seg["start"]),
                        "end": float(seg["end"]),
                        "speaker": "SPEAKER_01",
                        "text": str(seg.get("text") or ""),
                        "logprob": seg.get("avg_logprob"),
                    }
                )
            except Exception:
                continue

    # 4) translate.translate_lines (when needed)
    subs_srt_path: Path | None = srt_out
    if no_translate:
        logger.info("[v2] translate: disabled (--no-translate)")
        try:
            # Optional alignment even in no-translate mode (use source text).
            try:
                a_cfg = AlignConfig(aligner=aligner.lower(), max_stretch=float(max_stretch))
                segs_for_align = []
                for s in segments_for_mt:
                    s2 = dict(s)
                    s2["align_text"] = str(s2.get("text") or "").strip()
                    segs_for_align.append(s2)
                aligned = realign_srt(extracted, segs_for_align, a_cfg)
                segments_for_mt = [
                    {**orig, "start": float(al.get("start", orig.get("start", 0.0))), "end": float(al.get("end", orig.get("end", 0.0))), "aligned_by": al.get("aligned_by")}
                    for orig, al in zip(segments_for_mt, aligned)
                ]
                aligned_srt = out_dir / f"{stem}.aligned.srt"
                _write_srt_from_lines(
                    [{"start": s["start"], "end": s["end"], "speaker_id": s.get("speaker", "SPEAKER_01"), "text": s.get("text", "")} for s in segments_for_mt],
                    aligned_srt,
                )
                subs_srt_path = aligned_srt
                logger.info("[v2] align: ok (no-translate) segments=%s aligner=%s", len(segments_for_mt), aligner)
            except Exception as ex:
                logger.warning("[v2] align: skipped/failed (no-translate) (%s)", ex)

            # Keep segments for downstream (TTS windowing)
            write_json(translated_json, {"src_lang": src_lang, "tgt_lang": tgt_lang, "segments": segments_for_mt})
        except Exception:
            pass
    else:
        t_stage = time.perf_counter()
        try:
            cfg = TranslationConfig(
                mt_engine=mt_engine.lower(),
                mt_lowconf_thresh=float(mt_lowconf_thresh),
                glossary_path=glossary,
                style_path=style,
                show_id=show_id or video.stem,
                whisper_model=chosen_model,
                audio_path=str(extracted),
                device=chosen_device,
            )
            translated_segments = translate_segments(segments_for_mt, src_lang=src_lang, tgt_lang=tgt_lang, cfg=cfg)

            # Optional forced alignment to original audio (improves subtitle and TTS windowing).
            try:
                a_cfg = AlignConfig(aligner=aligner.lower(), max_stretch=float(max_stretch))
                # Provide "align_text" hint: prefer JP source text when present.
                segs_for_align = []
                for s in translated_segments:
                    s2 = dict(s)
                    align_text = str(s2.get("src_text") or "").strip()
                    if not align_text:
                        align_text = str(s2.get("text") or "").strip()
                    s2["align_text"] = align_text
                    segs_for_align.append(s2)
                aligned = realign_srt(extracted, segs_for_align, a_cfg)
                # Update timings on translated segments
                by_key = {(float(s.get("start", 0.0)), float(s.get("end", 0.0)), str(s.get("speaker", ""))): s for s in translated_segments}
                # Since aeneas may re-time drastically, just take aligned list ordering
                translated_segments = [
                    {**orig, "start": float(al.get("start", orig.get("start", 0.0))), "end": float(al.get("end", orig.get("end", 0.0))), "aligned_by": al.get("aligned_by")}
                    for orig, al in zip(translated_segments, aligned)
                ]
                logger.info("[v2] align: ok segments=%s aligner=%s", len(translated_segments), aligner)
            except Exception as ex:
                logger.warning("[v2] align: skipped/failed (%s)", ex)

            write_json(translated_json, {"src_lang": src_lang, "tgt_lang": tgt_lang, "segments": translated_segments})

            # Convert to SRT lines (speaker preserved; text from translated)
            srt_lines = [{"start": s["start"], "end": s["end"], "speaker_id": s["speaker"], "text": s["text"]} for s in translated_segments]
            _write_srt_from_lines(srt_lines, translated_srt)
            subs_srt_path = translated_srt
            logger.info("[v2] translate: segments=%s (%.2fs) → %s", len(translated_segments), time.perf_counter() - t_stage, translated_json)
        except Exception as ex:
            logger.exception("[v2] translate failed (continuing with original text): %s", ex)
            try:
                write_json(translated_json, {"src_lang": src_lang, "tgt_lang": tgt_lang, "segments": segments_for_mt, "error": str(ex)})
            except Exception:
                pass

    # 5) tts.synthesize (line-aligned)
    t_stage = time.perf_counter()
    try:
        tts.run(out_dir=out_dir, translated_json=translated_json, diarization_json=diar_json, wav_out=tts_wav, max_stretch=float(max_stretch))
        logger.info("[v2] tts: ok (%.2fs) → %s", time.perf_counter() - t_stage, tts_wav)
    except Exception as ex:
        logger.exception("[v2] tts failed (continuing with silence track): %s", ex)
        # Best-effort: create a silence wav of full duration using cue ends
        try:
            from anime_v2.stages.tts import _write_silence_wav  # type: ignore

            dur = max((float(s["end"]) for s in segments_for_mt), default=0.0)
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
