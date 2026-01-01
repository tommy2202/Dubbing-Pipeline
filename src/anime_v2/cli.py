from __future__ import annotations

import subprocess
import time
from pathlib import Path

import click
from click.exceptions import UsageError

from anime_v2.config import get_settings
from anime_v2.stages import audio_extractor, mkv_export, tts
from anime_v2.stages.align import AlignConfig, realign_srt
from anime_v2.stages.character_store import CharacterStore
from anime_v2.stages.diarization import DiarizeConfig
from anime_v2.stages.diarization import diarize as diarize_v2
from anime_v2.stages.mixing import MixConfig, mix
from anime_v2.stages.transcription import transcribe
from anime_v2.stages.translation import TranslationConfig, translate_segments
from anime_v2.utils.embeds import ecapa_embedding
from anime_v2.utils.io import read_json, write_json
from anime_v2.utils.log import logger
from anime_v2.utils.net import install_egress_policy
from anime_v2.utils.paths import output_dir_for
from anime_v2.utils.subtitles import write_vtt
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
        start_s, end_s = (p.strip() for p in lines[1].split("-->", 1))
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
        out.append(
            {
                "start": start,
                "end": end,
                "speaker_id": speaker_id,
                "text": str(c.get("text", "") or ""),
            }
        )
    return out


def _write_srt_from_lines(lines: list[dict], srt_path: Path) -> None:
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    with srt_path.open("w", encoding="utf-8") as f:
        for idx, line in enumerate(lines, 1):
            st = format_srt_timestamp(float(line["start"]))
            en = format_srt_timestamp(float(line["end"]))
            txt = str(line.get("text", "") or "").strip()
            f.write(f"{idx}\n{st} --> {en}\n{txt}\n\n")


def _write_vtt_from_lines(lines: list[dict], vtt_path: Path) -> None:
    # Reuse SRT parsing format (start/end/text)
    write_vtt(
        [
            {
                "start": float(line["start"]),
                "end": float(line["end"]),
                "text": str(line.get("text") or ""),
            }
            for line in lines
        ],
        vtt_path,
    )


@click.command()
@click.argument("video", required=False, type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--batch",
    "batch_spec",
    default=None,
    help="Batch input: directory path or glob pattern (e.g. 'Input/*.mp4')",
)
@click.option(
    "--jobs",
    type=int,
    default=1,
    show_default=True,
    help="Batch worker count (currently best-effort; batch runs sequentially by default)",
)
@click.option("--resume/--no-resume", default=True, show_default=True)
@click.option("--fail-fast/--no-fail-fast", default=False, show_default=True)
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
@click.option(
    "--asr-model",
    "asr_model_name",
    default=None,
    help="Override Whisper model name (e.g. large-v3, medium, small)",
)
@click.option(
    "--src-lang", default="auto", show_default=True, help="Source language code, or 'auto'"
)
@click.option(
    "--tgt-lang",
    default="en",
    show_default=True,
    help="Target language code (translate pathway outputs English)",
)
@click.option(
    "--no-translate", is_flag=True, default=False, help="Disable translate; do plain transcription"
)
@click.option(
    "--mt-provider",
    type=click.Choice(["auto", "whisper", "marian", "nllb", "none"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="Translation provider selection (alias for --mt-engine/--no-translate)",
)
@click.option(
    "--no-subs", is_flag=True, default=False, help="Do not mux subtitles into output container"
)
@click.option(
    "--subs",
    type=click.Choice(["off", "src", "tgt", "both"], case_sensitive=False),
    default="both",
    show_default=True,
    help="Which subtitles to write to disk (muxing controlled by --no-subs)",
)
@click.option(
    "--subs-format",
    type=click.Choice(["srt", "vtt", "both"], case_sensitive=False),
    default="srt",
    show_default=True,
    help="Subtitle file formats to write",
)
@click.option(
    "--diarizer",
    type=click.Choice(["auto", "pyannote", "speechbrain", "heuristic"], case_sensitive=False),
    default="auto",
    show_default=True,
)
@click.option(
    "--show-id",
    default=None,
    help="Show id used for persistent character IDs (default: input basename)",
)
@click.option(
    "--char-sim-thresh",
    type=float,
    default=float(get_settings().char_sim_thresh),
    show_default=True,
)
@click.option(
    "--mt-engine",
    type=click.Choice(["auto", "whisper", "marian", "nllb"], case_sensitive=False),
    default="auto",
    show_default=True,
)
@click.option(
    "--mt-lowconf-thresh",
    type=float,
    default=float(get_settings().mt_lowconf_thresh),
    show_default=True,
    help="Avg logprob threshold for Whisper translate fallback",
)
@click.option("--glossary", default=None, help="Glossary TSV path or directory")
@click.option("--style", default=None, help="Style YAML path or directory")
@click.option(
    "--aligner",
    type=click.Choice(["auto", "aeneas", "heuristic"], case_sensitive=False),
    default="auto",
    show_default=True,
)
@click.option(
    "--align-mode",
    type=click.Choice(["basic", "stretch", "word"], case_sensitive=False),
    default="stretch",
    show_default=True,
    help="Alignment detail: 'word' requests word timestamps when supported",
)
@click.option(
    "--max-stretch",
    type=float,
    default=float(get_settings().max_stretch),
    show_default=True,
    help="Max +/- time-stretch applied to TTS clips",
)
@click.option(
    "--mix-profile",
    type=click.Choice(["streaming", "broadcast", "simple"], case_sensitive=False),
    default=str(get_settings().mix_profile),
    show_default=True,
)
@click.option(
    "--separate-vocals/--no-separate-vocals",
    default=bool(get_settings().separate_vocals),
    show_default=True,
)
@click.option(
    "--separation",
    type=click.Choice(["off", "demucs"], case_sensitive=False),
    default=str(get_settings().separation),
    show_default=True,
)
@click.option("--separation-model", default=str(get_settings().separation_model), show_default=True)
@click.option(
    "--separation-device",
    type=click.Choice(["auto", "cpu", "cuda"], case_sensitive=False),
    default=str(get_settings().separation_device),
    show_default=True,
)
@click.option(
    "--mix",
    "mix_mode",
    type=click.Choice(["legacy", "enhanced"], case_sensitive=False),
    default=str(get_settings().mix_mode),
    show_default=True,
)
@click.option(
    "--lufs-target", type=float, default=float(get_settings().lufs_target), show_default=True
)
@click.option("--ducking/--no-ducking", default=bool(get_settings().ducking), show_default=True)
@click.option(
    "--ducking-strength",
    type=float,
    default=float(get_settings().ducking_strength),
    show_default=True,
)
@click.option("--limiter/--no-limiter", default=bool(get_settings().limiter), show_default=True)
@click.option(
    "--emit",
    default=str(get_settings().emit_formats),
    show_default=True,
    help="Comma list (always includes mkv,mp4): mkv,mp4,fmp4,hls",
)
@click.option(
    "--emotion-mode",
    type=click.Choice(["off", "auto", "tags"], case_sensitive=False),
    default=str(get_settings().emotion_mode),
    show_default=True,
    help="Expressive speech controls (best-effort, offline-friendly)",
)
@click.option(
    "--speech-rate", type=float, default=float(get_settings().speech_rate), show_default=True
)
@click.option("--pitch", type=float, default=float(get_settings().pitch), show_default=True)
@click.option("--energy", type=float, default=float(get_settings().energy), show_default=True)
@click.option("--realtime/--no-realtime", default=False, show_default=True)
@click.option("--chunk-seconds", type=float, default=20.0, show_default=True)
@click.option("--chunk-overlap", type=float, default=2.0, show_default=True)
@click.option("--stitch/--no-stitch", default=True, show_default=True)
@click.option(
    "--voice-mode",
    type=click.Choice(["clone", "preset", "single"], case_sensitive=False),
    default=str(get_settings().voice_mode),
    show_default=True,
)
@click.option("--voice-ref-dir", type=click.Path(path_type=Path), default=None)
@click.option("--voice-store", "voice_store_dir", type=click.Path(path_type=Path), default=None)
@click.option(
    "--tts-provider",
    type=click.Choice(["auto", "xtts", "basic", "espeak"], case_sensitive=False),
    default=str(get_settings().tts_provider),
    show_default=True,
)
@click.option(
    "--print-config",
    is_flag=True,
    default=False,
    help="Print a safe config report (no secrets) and exit",
)
@click.option("--dry-run", is_flag=True, default=False, help="Validate inputs/tools and exit")
@click.option("--verbose", is_flag=True, default=False, help="Verbose logging (INFO)")
@click.option("--debug", is_flag=True, default=False, help="Debug logging (DEBUG)")
def cli(
    video: Path | None,
    batch_spec: str | None,
    jobs: int,
    resume: bool,
    fail_fast: bool,
    device: str,
    mode: str,
    asr_model_name: str | None,
    src_lang: str,
    tgt_lang: str,
    no_translate: bool,
    mt_provider: str,
    no_subs: bool,
    subs: str,
    subs_format: str,
    diarizer: str,
    show_id: str | None,
    char_sim_thresh: float,
    mt_engine: str,
    mt_lowconf_thresh: float,
    glossary: str | None,
    style: str | None,
    aligner: str,
    align_mode: str,
    max_stretch: float,
    mix_profile: str,
    separate_vocals: bool,
    separation: str,
    separation_model: str,
    separation_device: str,
    mix_mode: str,
    lufs_target: float,
    ducking: bool,
    ducking_strength: float,
    limiter: bool,
    emit: str,
    emotion_mode: str,
    speech_rate: float,
    pitch: float,
    energy: float,
    realtime: bool,
    chunk_seconds: float,
    chunk_overlap: float,
    stitch: bool,
    voice_mode: str,
    voice_ref_dir: Path | None,
    voice_store_dir: Path | None,
    tts_provider: str,
    print_config: bool,
    dry_run: bool,
    verbose: bool,
    debug: bool,
) -> None:
    """
    Run pipeline-v2 on VIDEO.

    Example:
      anime-v2 Input/Test.mp4 --mode high --device auto
    """
    if print_config:
        import json as _json

        from config.settings import get_safe_config_report

        click.echo(_json.dumps(get_safe_config_report(), indent=2, sort_keys=True))
        return

    # CLI log verbosity controls (default behavior unchanged when flags not passed)
    if debug:
        from anime_v2.utils.log import set_log_level

        set_log_level("DEBUG")
    elif verbose:
        from anime_v2.utils.log import set_log_level

        set_log_level("INFO")

    if batch_spec and video is not None:
        raise UsageError("Provide either VIDEO or --batch, not both.")
    if not batch_spec and video is None:
        raise UsageError("Missing VIDEO (or pass --batch).")

    if batch_spec:
        import glob
        import json
        import sys
        import tempfile
        from concurrent.futures import ThreadPoolExecutor, as_completed

        spec = str(batch_spec)
        p = Path(spec)
        if p.exists() and p.is_dir():
            exts = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
            paths = sorted([x for x in p.rglob("*") if x.is_file() and x.suffix.lower() in exts])
        else:
            paths = sorted([Path(x) for x in glob.glob(spec)])

        if not paths:
            raise click.ClickException(f"No input files matched batch spec: {batch_spec!r}")

        jobs_n = max(1, int(jobs or 1))

        # Build per-job CLI args to run in isolated worker processes.
        base_args: list[str] = []
        base_args += ["--device", str(device)]
        base_args += ["--mode", str(mode)]
        if asr_model_name:
            base_args += ["--asr-model", str(asr_model_name)]
        base_args += ["--src-lang", str(src_lang)]
        base_args += ["--tgt-lang", str(tgt_lang)]
        if no_translate:
            base_args += ["--no-translate"]
        if str(mt_provider).lower() != "auto":
            base_args += ["--mt-provider", str(mt_provider)]
        if no_subs:
            base_args += ["--no-subs"]
        base_args += ["--subs", str(subs)]
        base_args += ["--subs-format", str(subs_format)]
        base_args += ["--diarizer", str(diarizer)]
        if show_id:
            base_args += ["--show-id", str(show_id)]
        base_args += ["--char-sim-thresh", str(char_sim_thresh)]
        base_args += ["--mt-engine", str(mt_engine)]
        base_args += ["--mt-lowconf-thresh", str(mt_lowconf_thresh)]
        if glossary:
            base_args += ["--glossary", str(glossary)]
        if style:
            base_args += ["--style", str(style)]
        base_args += ["--aligner", str(aligner)]
        base_args += ["--max-stretch", str(max_stretch)]
        base_args += ["--mix-profile", str(mix_profile)]
        base_args += ["--emit", str(emit)]
        if separate_vocals:
            base_args += ["--separate-vocals"]
        else:
            base_args += ["--no-separate-vocals"]

        # Run sequentially in-process when jobs=1 (keeps logs/stacktraces nicer).
        if jobs_n == 1:
            for idx, vp in enumerate(paths, 1):
                if not vp.exists():
                    msg = f"[batch {idx}/{len(paths)}] missing file: {vp}"
                    if fail_fast:
                        raise click.ClickException(msg)
                    logger.warning(msg)
                    continue
                out_dir_b = output_dir_for(vp)
                dub_mkv_b = out_dir_b / "dub.mkv"
                if resume and dub_mkv_b.exists():
                    logger.info("[batch %s/%s] skip (resume hit): %s", idx, len(paths), vp)
                    continue
                logger.info("[batch %s/%s] start: %s", idx, len(paths), vp)
                ctx = click.get_current_context()
                ctx.invoke(
                    cli,
                    video=vp,
                    batch_spec=None,
                    jobs=1,
                    resume=resume,
                    fail_fast=fail_fast,
                    device=device,
                    mode=mode,
                    src_lang=src_lang,
                    tgt_lang=tgt_lang,
                    no_translate=no_translate,
                    no_subs=no_subs,
                    subs=subs,
                    subs_format=subs_format,
                    diarizer=diarizer,
                    show_id=show_id,
                    char_sim_thresh=char_sim_thresh,
                    mt_engine=mt_engine,
                    mt_lowconf_thresh=mt_lowconf_thresh,
                    glossary=glossary,
                    style=style,
                    aligner=aligner,
                    max_stretch=max_stretch,
                    mix_profile=mix_profile,
                    separate_vocals=separate_vocals,
                    emit=emit,
                    print_config=False,
                )
            return

        # jobs>1: execute isolated workers via subprocess for robustness.
        with tempfile.TemporaryDirectory(prefix="anime_v2_batch_") as td:
            td_p = Path(td)

            specs: list[tuple[int, Path, Path]] = []
            for idx, vp in enumerate(paths, 1):
                if not vp.exists():
                    msg = f"[batch {idx}/{len(paths)}] missing file: {vp}"
                    if fail_fast:
                        raise click.ClickException(msg)
                    logger.warning(msg)
                    continue
                out_dir_b = output_dir_for(vp)
                dub_mkv_b = out_dir_b / "dub.mkv"
                if resume and dub_mkv_b.exists():
                    logger.info("[batch %s/%s] skip (resume hit): %s", idx, len(paths), vp)
                    continue

                spec_path = td_p / f"{idx:05d}.json"
                spec_path.write_text(
                    json.dumps({"args": [str(vp), *base_args]}, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                specs.append((idx, vp, spec_path))

            if not specs:
                logger.info("[v2] batch: nothing to do")
                return

            def _run_one(spec_path: Path) -> int:
                cmd = [sys.executable, "-m", "anime_v2.batch_worker", str(spec_path)]
                p = subprocess.run(cmd)
                return int(p.returncode)

            failures: list[str] = []
            with ThreadPoolExecutor(max_workers=jobs_n) as ex:
                futs = {ex.submit(_run_one, spec_path): (idx, vp) for idx, vp, spec_path in specs}
                for fut in as_completed(futs):
                    idx, vp = futs[fut]
                    rc = 1
                    try:
                        rc = int(fut.result())
                    except Exception as ex2:
                        rc = 1
                        logger.warning(
                            "[batch %s/%s] worker exception: %s (%s)", idx, len(paths), vp, ex2
                        )
                    if rc != 0:
                        failures.append(f"{vp} (exit={rc})")
                        logger.warning(
                            "[batch %s/%s] failed: %s (exit=%s)", idx, len(paths), vp, rc
                        )
                        if fail_fast:
                            break
                    else:
                        logger.info("[batch %s/%s] ok: %s", idx, len(paths), vp)

            if failures and fail_fast:
                raise click.ClickException("Batch failed (fail-fast):\n" + "\n".join(failures))
            if failures:
                logger.warning("Batch completed with failures:\n%s", "\n".join(failures))
            return

    assert video is not None
    if not video.exists():
        raise click.ClickException(f"Video not found: {video}")

    if dry_run:
        # Preflight checks: ffmpeg/ffprobe availability and output writability.
        try:
            from anime_v2.utils.ffmpeg_safe import ffprobe_duration_seconds

            _ = ffprobe_duration_seconds(video, timeout_s=10)
        except Exception as ex:
            raise click.ClickException(f"ffprobe failed: {ex}") from ex
        try:
            out_dir = output_dir_for(video)
            out_dir.mkdir(parents=True, exist_ok=True)
            test = out_dir / ".dry_run_write_test"
            test.write_text("ok", encoding="utf-8")
            test.unlink(missing_ok=True)
        except Exception as ex:
            raise click.ClickException(f"Output directory not writable: {ex}") from ex
        click.echo("DRY_RUN_OK")
        return

    # Enforce OFFLINE_MODE / ALLOW_EGRESS policy early.
    install_egress_policy()

    mode = mode.lower()
    chosen_model = str(asr_model_name).strip() if asr_model_name else MODE_TO_MODEL[mode]
    chosen_device = _select_device(device)

    # Provider selection aliases (keep backwards compatibility with existing flags).
    mt_provider_eff = str(mt_provider or "auto").lower()
    if mt_provider_eff != "auto":
        if mt_provider_eff == "none":
            no_translate = True
        else:
            no_translate = False
            mt_engine = mt_provider_eff

    # Output layout requirement:
    # Output/<video_stem>/{wav,srt,tts.wav,dub.mkv}
    stem = video.stem
    out_dir = output_dir_for(video)
    out_dir.mkdir(parents=True, exist_ok=True)

    wav_path = out_dir / "audio.wav"
    srt_out = out_dir / f"{stem}.srt"
    vtt_out = out_dir / f"{stem}.vtt"
    translated_srt = out_dir / f"{stem}.translated.srt"
    translated_vtt = out_dir / f"{stem}.translated.vtt"
    diar_json = out_dir / "diarization.json"
    translated_json = out_dir / "translated.json"
    tts_wav = out_dir / f"{stem}.tts.wav"
    dub_mkv = out_dir / "dub.mkv"

    logger.info(
        "[v2] Starting dub: video=%s mode=%s model=%s device=%s",
        video,
        mode,
        chosen_model,
        chosen_device,
    )
    logger.info(
        "[v2] Languages: src_lang=%s tgt_lang=%s translate=%s",
        src_lang,
        tgt_lang,
        (not no_translate),
    )

    # Realtime (pseudo-streaming) mode: chunked pipeline with per-chunk artifacts.
    if realtime:
        from anime_v2.realtime import realtime_dub

        realtime_dub(
            video=video,
            out_dir=out_dir,
            device=chosen_device,
            asr_model=chosen_model,
            src_lang=src_lang,
            tgt_lang=tgt_lang,
            mt_engine=mt_engine,
            mt_lowconf_thresh=float(mt_lowconf_thresh),
            glossary=glossary,
            style=style,
            chunk_seconds=float(chunk_seconds),
            chunk_overlap=float(chunk_overlap),
            stitch=bool(stitch),
            subs_choice=(subs or "both").lower(),
            subs_format=(subs_format or "srt").lower(),
            align_mode=(align_mode or "stretch").lower(),
            emotion_mode=emotion_mode,
            speech_rate=float(speech_rate),
            pitch=float(pitch),
            energy=float(energy),
        )
        return

    t0 = time.perf_counter()

    # 1) audio_extractor.extract
    extracted: Path | None = None
    t_stage = time.perf_counter()
    try:
        extracted = audio_extractor.extract(video=video, out_dir=out_dir, wav_out=wav_path)
        logger.info(
            "[v2] audio_extractor: ok path=%s (%.2fs)", extracted, time.perf_counter() - t_stage
        )
    except Exception as ex:
        logger.exception("[v2] audio_extractor failed: %s", ex)
        logger.error("[v2] Done. Output: (failed early)")
        return

    # Optional Tier-1 A separation (opt-in; default off)
    stems_dir = out_dir / "stems"
    audio_dir = out_dir / "audio"
    stems_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)
    background_wav: Path | None = None
    if str(mix_mode).lower() == "enhanced":
        sep_mode = str(separation or "off").lower()
        if sep_mode == "demucs":
            try:
                from anime_v2.audio.separation import separate_dialogue

                res = separate_dialogue(
                    Path(str(extracted)),
                    stems_dir,
                    model=str(separation_model),
                    device=str(separation_device),
                )
                background_wav = res.background_wav
            except Exception as ex:
                logger.warning(
                    "[v2] separation requested but unavailable; falling back to no separation (%s)",
                    ex,
                )
                background_wav = Path(str(extracted))
        else:
            background_wav = Path(str(extracted))

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
                from anime_v2.utils.ffmpeg_safe import extract_audio_mono_16k

                extract_audio_mono_16k(
                    src=Path(str(extracted)),
                    dst=seg_wav,
                    start_s=float(s),
                    end_s=float(e),
                    timeout_s=120,
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
                        "conf": float(
                            next((u["conf"] for u in utts if str(u["speaker"]) == lab), 0.0)
                        ),
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
        logger.info(
            "[v2] diarize: diar_segments=%s stable_speakers=%s (%.2fs)",
            len(diar_segments),
            len(set(s.get("speaker_id") for s in diar_segments)),
            time.perf_counter() - t_stage,
        )
    except Exception as ex:
        logger.exception("[v2] diarize failed (continuing): %s", ex)

    # 3) transcription.transcribe (ASR-only)
    t_stage = time.perf_counter()
    trans_meta_path = srt_out.with_suffix(".json")
    try:
        want_words = str(align_mode or "").lower() == "word"
        transcribe(
            audio_path=extracted,
            srt_out=srt_out,
            device=chosen_device,
            model_name=chosen_model,
            task="transcribe",
            src_lang=src_lang,
            tgt_lang=tgt_lang,
            word_timestamps=want_words,
        )
        meta = {}
        try:
            meta = read_json(trans_meta_path, default={})  # type: ignore[assignment]
        except Exception:
            meta = {}
        segs_detail = meta.get("segments_detail", []) if isinstance(meta, dict) else []
        cues = segs_detail if isinstance(segs_detail, list) else []
        logger.info(
            "[v2] transcription: segments=%s (%.2fs) → %s",
            len(cues),
            time.perf_counter() - t_stage,
            srt_out,
        )
    except Exception as ex:
        logger.exception("[v2] transcription failed (continuing): %s", ex)
        cues = []

    # Optional subtitle format conversions for source transcript
    subs_choice = (subs or "both").lower()
    subs_fmt = (subs_format or "srt").lower()
    if subs_choice in {"src", "both"} and subs_fmt in {"vtt", "both"}:
        try:
            write_vtt(_parse_srt_to_cues(srt_out), vtt_out)
        except Exception as ex:
            logger.warning("[v2] vtt export failed (src): %s", ex)

    # Build speaker-timed segments.
    # Prefer diarization utterances (preserve diarization timing) and assign text/logprob from transcription overlaps.
    diar_utts = sorted(
        [
            {
                "start": float(s["start"]),
                "end": float(s["end"]),
                "speaker": str(s.get("speaker_id") or "SPEAKER_01"),
            }
            for s in diar_segments
        ],
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
                    logprob = sum(lp * w for lp, w in zip(lp_parts, w_parts, strict=False)) / tot
            segments_for_mt.append(
                {
                    "start": u["start"],
                    "end": u["end"],
                    "speaker": u["speaker"],
                    "text": text_src,
                    "logprob": logprob,
                }
            )
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
                    {
                        **orig,
                        "start": float(al.get("start", orig.get("start", 0.0))),
                        "end": float(al.get("end", orig.get("end", 0.0))),
                        "aligned_by": al.get("aligned_by"),
                    }
                    for orig, al in zip(segments_for_mt, aligned, strict=False)
                ]
                aligned_srt = out_dir / f"{stem}.aligned.srt"
                _write_srt_from_lines(
                    [
                        {
                            "start": s["start"],
                            "end": s["end"],
                            "speaker_id": s.get("speaker", "SPEAKER_01"),
                            "text": s.get("text", ""),
                        }
                        for s in segments_for_mt
                    ],
                    aligned_srt,
                )
                subs_srt_path = aligned_srt
                if subs_choice in {"src", "both"} and subs_fmt in {"vtt", "both"}:
                    try:
                        _write_vtt_from_lines(
                            [
                                {
                                    "start": s["start"],
                                    "end": s["end"],
                                    "text": s.get("text", ""),
                                }
                                for s in segments_for_mt
                            ],
                            out_dir / f"{stem}.aligned.vtt",
                        )
                    except Exception as ex:
                        logger.warning("[v2] vtt export failed (aligned src): %s", ex)
                logger.info(
                    "[v2] align: ok (no-translate) segments=%s aligner=%s",
                    len(segments_for_mt),
                    aligner,
                )
            except Exception as ex:
                logger.warning("[v2] align: skipped/failed (no-translate) (%s)", ex)

            # Keep segments for downstream (TTS windowing)
            write_json(
                translated_json,
                {"src_lang": src_lang, "tgt_lang": tgt_lang, "segments": segments_for_mt},
            )
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
            translated_segments = translate_segments(
                segments_for_mt, src_lang=src_lang, tgt_lang=tgt_lang, cfg=cfg
            )

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
                # Since aeneas may re-time drastically, just take aligned list ordering
                translated_segments = [
                    {
                        **orig,
                        "start": float(al.get("start", orig.get("start", 0.0))),
                        "end": float(al.get("end", orig.get("end", 0.0))),
                        "aligned_by": al.get("aligned_by"),
                    }
                    for orig, al in zip(translated_segments, aligned, strict=False)
                ]
                logger.info(
                    "[v2] align: ok segments=%s aligner=%s", len(translated_segments), aligner
                )
            except Exception as ex:
                logger.warning("[v2] align: skipped/failed (%s)", ex)

            write_json(
                translated_json,
                {"src_lang": src_lang, "tgt_lang": tgt_lang, "segments": translated_segments},
            )

            # Convert to SRT lines (speaker preserved; text from translated)
            srt_lines = [
                {
                    "start": s["start"],
                    "end": s["end"],
                    "speaker_id": s["speaker"],
                    "text": s["text"],
                }
                for s in translated_segments
            ]
            _write_srt_from_lines(srt_lines, translated_srt)
            subs_srt_path = translated_srt
            if subs_choice in {"tgt", "both"} and subs_fmt in {"vtt", "both"}:
                try:
                    _write_vtt_from_lines(
                        [
                            {"start": s["start"], "end": s["end"], "text": s["text"]}
                            for s in translated_segments
                        ],
                        translated_vtt,
                    )
                except Exception as ex:
                    logger.warning("[v2] vtt export failed (tgt): %s", ex)
            logger.info(
                "[v2] translate: segments=%s (%.2fs) → %s",
                len(translated_segments),
                time.perf_counter() - t_stage,
                translated_json,
            )
        except Exception as ex:
            logger.exception("[v2] translate failed (continuing with original text): %s", ex)
            from contextlib import suppress

            with suppress(Exception):
                write_json(
                    translated_json,
                    {
                        "src_lang": src_lang,
                        "tgt_lang": tgt_lang,
                        "segments": segments_for_mt,
                        "error": str(ex),
                    },
                )

    # 5) tts.synthesize (line-aligned)
    t_stage = time.perf_counter()
    try:
        # TTS language: default to target language when translating; otherwise prefer source language.
        tts_lang = None
        if no_translate:
            if str(src_lang).lower() != "auto":
                tts_lang = str(src_lang)
        else:
            tts_lang = str(tgt_lang) if tgt_lang else None
        tts.run(
            out_dir=out_dir,
            translated_json=translated_json,
            diarization_json=diar_json,
            wav_out=tts_wav,
            tts_lang=tts_lang,
            voice_mode=voice_mode,
            voice_ref_dir=voice_ref_dir,
            voice_store_dir=voice_store_dir,
            tts_provider=tts_provider,
            emotion_mode=emotion_mode,
            speech_rate=float(speech_rate),
            pitch=float(pitch),
            energy=float(energy),
            max_stretch=float(max_stretch),
        )
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

    # 6) mix (broadcast-quality) + emit mkv/mp4
    t_stage = time.perf_counter()
    try:
        # Always include mkv+mp4, plus requested.
        requested = {p.strip().lower() for p in (emit or "").split(",") if p.strip()}
        requested |= {"mkv", "mp4"}
        emit_set = tuple(sorted(requested))
        if str(mix_mode).lower() == "enhanced":
            # Tier-1 A enhanced mixing uses extracted/separated background + TTS dialogue
            from anime_v2.audio.mix import MixParams, mix_dubbed_audio
            from anime_v2.stages.export import export_hls, export_mkv, export_mp4

            bg = background_wav or Path(str(extracted))
            final_mix = audio_dir / "final_mix.wav"
            mix_dubbed_audio(
                background_wav=bg,
                tts_dialogue_wav=tts_wav,
                out_wav=final_mix,
                params=MixParams(
                    lufs_target=float(lufs_target),
                    ducking=bool(ducking),
                    ducking_strength=float(ducking_strength),
                    limiter=bool(limiter),
                ),
            )
            outs: dict[str, Path] = {}
            if "mkv" in emit_set:
                outs["mkv"] = export_mkv(
                    video, final_mix, None if no_subs else subs_srt_path, out_dir / "dub.mkv"
                )
            if "mp4" in emit_set:
                outs["mp4"] = export_mp4(
                    video, final_mix, None if no_subs else subs_srt_path, out_dir / "dub.mp4"
                )
            if "fmp4" in emit_set:
                outs["fmp4"] = export_mp4(
                    video,
                    final_mix,
                    None if no_subs else subs_srt_path,
                    out_dir / "dub.frag.mp4",
                    fragmented=True,
                )
            if "hls" in emit_set:
                outs["hls"] = export_hls(
                    video, final_mix, None if no_subs else subs_srt_path, out_dir / "hls"
                )
        else:
            cfg_mix = MixConfig(
                profile=mix_profile.lower(), separate_vocals=bool(separate_vocals), emit=emit_set
            )
            outs = mix(
                video_in=video,
                tts_wav=tts_wav,
                srt=None if no_subs else subs_srt_path,
                out_dir=out_dir,
                cfg=cfg_mix,
            )
        if "mkv" in outs:
            dub_mkv = outs["mkv"]
        if "mp4" in outs:
            _ = outs["mp4"]
        logger.info(
            "[v2] mix: ok (%.2fs) → %s",
            time.perf_counter() - t_stage,
            ", ".join(str(p) for p in outs.values()),
        )
    except Exception as ex:
        logger.exception("[v2] mix failed; falling back to simple mux: %s", ex)
        try:
            mkv_export.run(
                video=video,
                dubbed_audio=tts_wav,
                srt_path=None if no_subs else subs_srt_path,
                mkv_out=dub_mkv,
                ckpt_dir=out_dir,
                out_dir=out_dir,
            )
            logger.info("[v2] mux fallback: ok → %s", dub_mkv)
        except Exception as ex2:
            logger.exception("[v2] mux fallback failed: %s", ex2)
            logger.error("[v2] Done. Output: (mux failed)")
            return

    logger.info("[v2] Done in %.2fs", time.perf_counter() - t0)
    logger.info("[v2] Done. Output: %s", dub_mkv)


if __name__ == "__main__":  # pragma: no cover
    cli()
