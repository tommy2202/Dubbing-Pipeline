from __future__ import annotations

import subprocess
import time
from contextlib import suppress
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


class DefaultGroup(click.Group):
    """
    Click group that supports a default command.

    This preserves backwards-compatible usage:
      anime-v2 Input/Test.mp4 ...
    while enabling subcommands:
      anime-v2 review ...
    """

    def __init__(self, *args, default_cmd: str = "run", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.default_cmd = str(default_cmd)

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args:
            first = args[0]
            if first not in self.commands:
                args.insert(0, self.default_cmd)
        return super().parse_args(ctx, args)


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
    from anime_v2.utils.cues import parse_srt_to_cues

    return parse_srt_to_cues(srt_path)


def _assign_speakers(cues: list[dict], diar_segments: list[dict] | None) -> list[dict]:
    from anime_v2.utils.cues import assign_speakers

    return assign_speakers(cues, diar_segments)


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


@click.command(name="run")
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
@click.option("--project", "project_name", default=None, help="Project profile name under projects/<name>/")
@click.option(
    "--style-guide",
    "style_guide_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Project style guide YAML/JSON (overrides --project).",
)
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
    "--timing-fit/--no-timing-fit",
    default=bool(get_settings().timing_fit),
    show_default=True,
)
@click.option("--pacing/--no-pacing", default=bool(get_settings().pacing), show_default=True)
@click.option(
    "--pacing-min-stretch",
    "--min-stretch",
    "pacing_min_stretch",
    type=float,
    default=float(get_settings().pacing_min_ratio),
    show_default=True,
)
@click.option(
    "--pacing-max-stretch",
    "--pace-max-stretch",
    "pacing_max_stretch",
    type=float,
    default=float(get_settings().pacing_max_ratio),
    show_default=True,
)
@click.option("--wps", type=float, default=float(get_settings().timing_wps), show_default=True)
@click.option(
    "--tolerance",
    type=float,
    default=float(get_settings().timing_tolerance),
    show_default=True,
    help="Timing tolerance as a fraction (e.g. 0.10 = 10%)",
)
@click.option("--timing-debug", is_flag=True, default=False, help="Write per-segment debug JSON")
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
    "--expressive",
    "expressive_mode",
    type=click.Choice(["off", "auto", "source-audio", "text-only"], case_sensitive=False),
    default=str(get_settings().expressive),
    show_default=True,
    help="Tier-3B expressive prosody guidance (opt-in)",
)
@click.option(
    "--expressive-strength",
    type=float,
    default=float(get_settings().expressive_strength),
    show_default=True,
    help="Expressive strength 0..1 (conservative range)",
)
@click.option(
    "--expressive-debug",
    is_flag=True,
    default=bool(get_settings().expressive_debug),
    show_default=True,
    help="Write Output/<job>/expressive/plans/<segment>.json",
)
@click.option(
    "--speech-rate", type=float, default=float(get_settings().speech_rate), show_default=True
)
@click.option("--pitch", type=float, default=float(get_settings().pitch), show_default=True)
@click.option("--energy", type=float, default=float(get_settings().energy), show_default=True)
@click.option("--realtime/--no-realtime", default=False, show_default=True)
@click.option(
    "--chunk-seconds", type=float, default=float(get_settings().stream_chunk_seconds), show_default=True
)
@click.option(
    "--chunk-overlap", type=float, default=float(get_settings().stream_overlap_seconds), show_default=True
)
@click.option(
    "--stream",
    "stream_mode",
    type=click.Choice(["off", "on"], case_sensitive=False),
    default=("on" if bool(get_settings().stream) else "off"),
    show_default=True,
    help="Tier-3C streaming mode (chunked). Alias: --realtime",
)
@click.option(
    "--overlap-seconds",
    "overlap_seconds",
    type=float,
    default=None,
    help="Alias for --chunk-overlap",
)
@click.option(
    "--stream-output",
    type=click.Choice(["segments", "final"], case_sensitive=False),
    default=str(get_settings().stream_output),
    show_default=True,
    help="Streaming output mode: per-chunk MP4s or stitched final MP4",
)
@click.option(
    "--stream-concurrency", type=int, default=int(get_settings().stream_concurrency), show_default=True
)
@click.option("--stitch/--no-stitch", default=True, show_default=True)
@click.option(
    "--music-detect",
    "music_detect",
    type=click.Choice(["off", "on"], case_sensitive=False),
    default=("on" if bool(get_settings().music_detect) else "off"),
    show_default=True,
)
@click.option(
    "--music-mode",
    type=click.Choice(["auto", "heuristic", "classifier"], case_sensitive=False),
    default=str(get_settings().music_mode),
    show_default=True,
)
@click.option(
    "--music-threshold",
    type=float,
    default=float(get_settings().music_threshold),
    show_default=True,
)
@click.option(
    "--op-ed-detect",
    "op_ed_detect",
    type=click.Choice(["off", "on"], case_sensitive=False),
    default=("on" if bool(get_settings().op_ed_detect) else "off"),
    show_default=True,
)
@click.option(
    "--op-ed-seconds",
    type=int,
    default=int(get_settings().op_ed_seconds),
    show_default=True,
)
@click.option(
    "--speaker-smoothing",
    "speaker_smoothing",
    type=click.Choice(["off", "on"], case_sensitive=False),
    default="off",
    show_default=True,
    help="Scene-aware post-processing to reduce diarization speaker flips (uses audio scene detection).",
)
@click.option(
    "--scene-detect",
    type=click.Choice(["off", "audio"], case_sensitive=False),
    default="audio",
    show_default=True,
    help="Scene boundary detection mode (only used when speaker smoothing is enabled).",
)
@click.option(
    "--smoothing-min-turn",
    type=float,
    default=float(get_settings().smoothing_min_turn_s),
    show_default=True,
)
@click.option(
    "--smoothing-surround-gap",
    type=float,
    default=float(get_settings().smoothing_surround_gap_s),
    show_default=True,
)
@click.option(
    "--pg",
    "pg_mode",
    type=click.Choice(["off", "pg13", "pg"], case_sensitive=False),
    default="off",
    show_default=True,
    help="Per-run PG mode applied after translation/style, before timing-fit/TTS/subs.",
)
@click.option(
    "--pg-policy",
    "pg_policy_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    show_default=False,
    help="Optional JSON policy overrides for PG filter (offline, deterministic).",
)
@click.option(
    "--qa",
    "qa_mode",
    type=click.Choice(["off", "on"], case_sensitive=False),
    default="off",
    show_default=True,
    help="Run offline quality checks after pipeline (writes Output/<job>/qa/*).",
)
@click.option(
    "--director",
    "director_mode",
    type=click.Choice(["off", "on"], case_sensitive=False),
    default="off",
    show_default=True,
    help="Dub Director mode: adds conservative expressive adjustments based on scene/intent.",
)
@click.option(
    "--director-strength",
    type=float,
    default=float(get_settings().director_strength),
    show_default=True,
)
@click.option(
    "--multitrack",
    "multitrack",
    type=click.Choice(["off", "on"], case_sensitive=False),
    default="off",
    show_default=True,
    help="Produce multi-track audio artifacts and mux multi-audio MKV (when container=mkv).",
)
@click.option(
    "--container",
    "container",
    type=click.Choice(["mkv", "mp4"], case_sensitive=False),
    default=str(get_settings().container),
    show_default=True,
    help="Primary container when --multitrack on (MP4 uses sidecar .m4a tracks).",
)
@click.option(
    "--voice-mode",
    type=click.Choice(["clone", "preset", "single"], case_sensitive=False),
    default=str(get_settings().voice_mode),
    show_default=True,
)
@click.option("--voice-ref-dir", type=click.Path(path_type=Path), default=None)
@click.option("--voice-store", "voice_store_dir", type=click.Path(path_type=Path), default=None)
@click.option(
    "--voice-memory",
    type=click.Choice(["off", "on"], case_sensitive=False),
    default="off",
    show_default=True,
    help="Tier-2A Character Voice Memory (cross-episode speaker stability)",
)
@click.option("--voice-memory-dir", type=click.Path(path_type=Path), default=None)
@click.option(
    "--voice-match-threshold",
    type=float,
    default=float(get_settings().voice_match_threshold),
    show_default=True,
)
@click.option(
    "--voice-auto-enroll/--no-voice-auto-enroll",
    default=bool(get_settings().voice_auto_enroll),
    show_default=True,
)
@click.option("--voice-character-map", type=click.Path(path_type=Path), default=None)
@click.option("--list-characters", is_flag=True, default=False, help="List voice-memory characters")
@click.option(
    "--rename-character",
    nargs=2,
    type=str,
    default=None,
    help="Rename a character: --rename-character <id> <name>",
)
@click.option(
    "--set-character-voice-mode",
    nargs=2,
    type=str,
    default=None,
    help="Set character voice mode: --set-character-voice-mode <id> clone|preset|single",
)
@click.option(
    "--set-character-preset",
    nargs=2,
    type=str,
    default=None,
    help="Set character preset voice: --set-character-preset <id> <preset_voice_id>",
)
@click.option(
    "--tts-provider",
    type=click.Choice(["auto", "xtts", "basic", "espeak"], case_sensitive=False),
    default=str(get_settings().tts_provider),
    show_default=True,
)
@click.option(
    "--lipsync",
    "lipsync_mode",
    type=click.Choice(["off", "wav2lip"], case_sensitive=False),
    default=str(get_settings().lipsync),
    show_default=True,
    help="Optional Tier-3A lip-sync plugin (post-process output video)",
)
@click.option("--wav2lip-dir", type=click.Path(path_type=Path), default=None)
@click.option("--wav2lip-checkpoint", type=click.Path(path_type=Path), default=None)
@click.option(
    "--lipsync-face",
    type=click.Choice(["auto", "center", "bbox"], case_sensitive=False),
    default=str(get_settings().lipsync_face),
    show_default=True,
)
@click.option(
    "--lipsync-device",
    type=click.Choice(["auto", "cpu", "cuda"], case_sensitive=False),
    default=str(get_settings().lipsync_device),
    show_default=True,
)
@click.option(
    "--lipsync-box",
    default=None,
    help="Face box override for Wav2Lip when --lipsync-face bbox: 'x1,y1,x2,y2'",
)
@click.option(
    "--strict-plugins",
    is_flag=True,
    default=False,
    help="Fail the job if a requested optional plugin is unavailable",
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
def run(
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
    project_name: str | None,
    style_guide_path: Path | None,
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
    timing_fit: bool,
    pacing: bool,
    pacing_min_stretch: float,
    pacing_max_stretch: float,
    wps: float,
    tolerance: float,
    timing_debug: bool,
    emit: str,
    emotion_mode: str,
    expressive_mode: str,
    expressive_strength: float,
    expressive_debug: bool,
    speech_rate: float,
    pitch: float,
    energy: float,
    realtime: bool,
    chunk_seconds: float,
    chunk_overlap: float,
    stream_mode: str,
    overlap_seconds: float | None,
    stream_output: str,
    stream_concurrency: int,
    stitch: bool,
    music_detect: str,
    music_mode: str,
    music_threshold: float,
    op_ed_detect: str,
    op_ed_seconds: int,
    speaker_smoothing: str,
    scene_detect: str,
    smoothing_min_turn: float,
    smoothing_surround_gap: float,
    pg_mode: str,
    pg_policy_path: Path | None,
    qa_mode: str,
    director_mode: str,
    director_strength: float,
    multitrack: str,
    container: str,
    voice_mode: str,
    voice_ref_dir: Path | None,
    voice_store_dir: Path | None,
    voice_memory: str,
    voice_memory_dir: Path | None,
    voice_match_threshold: float,
    voice_auto_enroll: bool,
    voice_character_map: Path | None,
    list_characters: bool,
    rename_character: tuple[str, str] | None,
    set_character_voice_mode: tuple[str, str] | None,
    set_character_preset: tuple[str, str] | None,
    tts_provider: str,
    lipsync_mode: str,
    wav2lip_dir: Path | None,
    wav2lip_checkpoint: Path | None,
    lipsync_face: str,
    lipsync_device: str,
    lipsync_box: str | None,
    strict_plugins: bool,
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

    # Strict plugins default from settings unless flag is set.
    if not strict_plugins:
        with suppress(Exception):
            strict_plugins = bool(get_settings().strict_plugins)

    # Tier-2A management commands (no pipeline run required)
    if list_characters or rename_character or set_character_voice_mode or set_character_preset:
        import json as _json

        from anime_v2.voice_memory.store import VoiceMemoryStore

        s = get_settings()
        root = Path(voice_memory_dir or s.voice_memory_dir).resolve()
        store = VoiceMemoryStore(root)
        if list_characters:
            for c in store.list_characters():
                click.echo(_json.dumps(c, sort_keys=True))
        if rename_character:
            cid, nm = rename_character
            store.rename_character(cid, nm)
            click.echo(f"OK rename {cid} -> {nm}")
        if set_character_voice_mode:
            cid, mode2 = set_character_voice_mode
            store.set_character_voice_mode(cid, mode2)
            click.echo(f"OK voice_mode {cid} -> {mode2}")
        if set_character_preset:
            cid, preset = set_character_preset
            store.set_character_preset(cid, preset)
            click.echo(f"OK preset {cid} -> {preset}")
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
        base_args += ["--mix", str(mix_mode)]
        base_args += ["--lufs-target", str(lufs_target)]
        if ducking:
            base_args += ["--ducking"]
        else:
            base_args += ["--no-ducking"]
        base_args += ["--ducking-strength", str(ducking_strength)]
        if limiter:
            base_args += ["--limiter"]
        else:
            base_args += ["--no-limiter"]
        base_args += ["--separation", str(separation)]
        base_args += ["--separation-model", str(separation_model)]
        base_args += ["--separation-device", str(separation_device)]
        if timing_fit:
            base_args += ["--timing-fit"]
        else:
            base_args += ["--no-timing-fit"]
        if pacing:
            base_args += ["--pacing"]
        else:
            base_args += ["--no-pacing"]
        base_args += ["--pacing-min-stretch", str(pacing_min_stretch)]
        base_args += ["--pacing-max-stretch", str(pacing_max_stretch)]
        base_args += ["--wps", str(wps)]
        base_args += ["--tolerance", str(tolerance)]
        if timing_debug:
            base_args += ["--timing-debug"]
        base_args += ["--voice-memory", str(voice_memory)]
        if voice_memory_dir:
            base_args += ["--voice-memory-dir", str(voice_memory_dir)]
        base_args += ["--voice-match-threshold", str(voice_match_threshold)]
        if voice_auto_enroll:
            base_args += ["--voice-auto-enroll"]
        else:
            base_args += ["--no-voice-auto-enroll"]
        if voice_character_map:
            base_args += ["--voice-character-map", str(voice_character_map)]
        if project_name:
            base_args += ["--project", str(project_name)]
        if style_guide_path:
            base_args += ["--style-guide", str(style_guide_path)]
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
                    run,
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
                    project_name=project_name,
                    style_guide_path=style_guide_path,
                    aligner=aligner,
                    max_stretch=max_stretch,
                    mix_profile=mix_profile,
                    separate_vocals=separate_vocals,
                    separation=separation,
                    separation_model=separation_model,
                    separation_device=separation_device,
                    mix_mode=mix_mode,
                    lufs_target=lufs_target,
                    ducking=ducking,
                    ducking_strength=ducking_strength,
                    limiter=limiter,
                    timing_fit=timing_fit,
                    pacing=pacing,
                    pacing_min_stretch=pacing_min_stretch,
                    pacing_max_stretch=pacing_max_stretch,
                    wps=wps,
                    tolerance=tolerance,
                    timing_debug=timing_debug,
                    voice_mode=voice_mode,
                    voice_ref_dir=voice_ref_dir,
                    voice_store_dir=voice_store_dir,
                    voice_memory=voice_memory,
                    voice_memory_dir=voice_memory_dir,
                    voice_match_threshold=voice_match_threshold,
                    voice_auto_enroll=voice_auto_enroll,
                    voice_character_map=voice_character_map,
                    list_characters=False,
                    rename_character=None,
                    set_character_voice_mode=None,
                    set_character_preset=None,
                    tts_provider=tts_provider,
                    lipsync_mode=lipsync_mode,
                    wav2lip_dir=wav2lip_dir,
                    wav2lip_checkpoint=wav2lip_checkpoint,
                    lipsync_face=lipsync_face,
                    lipsync_device=lipsync_device,
                    lipsync_box=lipsync_box,
                    strict_plugins=bool(strict_plugins),
                    emit=emit,
                    emotion_mode=emotion_mode,
                    expressive_mode=expressive_mode,
                    expressive_strength=expressive_strength,
                    expressive_debug=bool(expressive_debug),
                    speech_rate=speech_rate,
                    pitch=pitch,
                    energy=energy,
                    realtime=realtime,
                    chunk_seconds=chunk_seconds,
                    chunk_overlap=chunk_overlap,
                    stream_mode=stream_mode,
                    overlap_seconds=overlap_seconds,
                    stream_output=stream_output,
                    stream_concurrency=int(stream_concurrency),
                    stitch=stitch,
                    music_detect=music_detect,
                    music_mode=music_mode,
                    music_threshold=music_threshold,
                    op_ed_detect=op_ed_detect,
                    op_ed_seconds=op_ed_seconds,
                    speaker_smoothing=speaker_smoothing,
                    scene_detect=scene_detect,
                    smoothing_min_turn=smoothing_min_turn,
                    smoothing_surround_gap=smoothing_surround_gap,
                    pg_mode=pg_mode,
                    pg_policy_path=str(pg_policy_path) if pg_policy_path else None,
                    qa_mode=qa_mode,
                    director_mode=director_mode,
                    director_strength=float(director_strength),
                    multitrack=multitrack,
                    container=container,
                    print_config=False,
                    dry_run=False,
                    verbose=verbose,
                    debug=debug,
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

    # Tier-3C streaming mode: chunked pipeline with per-chunk MP4s (+ optional stitch).
    # Backwards compat: --realtime enables streaming mode.
    eff_stream = (str(stream_mode or "off").lower() == "on") or bool(realtime)
    if overlap_seconds is not None:
        chunk_overlap = float(overlap_seconds)
    if eff_stream:
        from anime_v2.streaming.runner import run_streaming

        out_mode = "final" if bool(stitch) else str(stream_output or "segments").lower()
        run_streaming(
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
            project=str(project_name) if project_name else None,
            style_guide_path=Path(style_guide_path).resolve() if style_guide_path else None,
            stream=True,
            chunk_seconds=float(chunk_seconds),
            overlap_seconds=float(chunk_overlap),
            stream_output=out_mode,
            stream_concurrency=int(stream_concurrency),
            timing_fit=bool(timing_fit),
            pacing=bool(pacing),
            pacing_min_ratio=float(pacing_min_stretch),
            pacing_max_ratio=float(pacing_max_stretch),
            timing_tolerance=float(tolerance),
            align_mode=str(align_mode or "stretch").lower(),
            emotion_mode=str(emotion_mode),
            expressive=str(expressive_mode),
            expressive_strength=float(expressive_strength),
            expressive_debug=bool(expressive_debug),
            speech_rate=float(speech_rate),
            pitch=float(pitch),
            energy=float(energy),
            music_detect=(str(music_detect).lower() == "on"),
            music_mode=str(music_mode).lower(),
            music_threshold=float(music_threshold),
            op_ed_detect=(str(op_ed_detect).lower() == "on"),
            op_ed_seconds=int(op_ed_seconds),
            pg=str(pg_mode).lower(),
            pg_policy_path=Path(pg_policy_path).resolve() if pg_policy_path else None,
            qa=(str(qa_mode).lower() == "on"),
            speaker_smoothing=(str(speaker_smoothing).lower() == "on"),
            scene_detect=str(scene_detect).lower(),
            smoothing_min_turn_s=float(smoothing_min_turn),
            smoothing_surround_gap_s=float(smoothing_surround_gap),
            director=(str(director_mode).lower() == "on"),
            director_strength=float(director_strength),
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

    # Tier-Next A/B: optional music/singing preservation analysis
    analysis_dir = out_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    music_regions_path = None
    if str(music_detect).lower() == "on":
        try:
            from anime_v2.audio.music_detect import (
                analyze_audio_for_music_regions,
                detect_op_ed,
                write_oped_json,
                write_regions_json,
            )

            regs = analyze_audio_for_music_regions(
                extracted,
                mode=str(music_mode).lower(),
                window_s=1.0,
                hop_s=0.5,
                threshold=float(music_threshold),
            )
            music_regions_path = analysis_dir / "music_regions.json"
            write_regions_json(regs, music_regions_path)
            logger.info(
                "music_detect_regions",
                regions=len(regs),
                path=str(music_regions_path),
                mode=str(music_mode).lower(),
                threshold=float(music_threshold),
            )
            if str(op_ed_detect).lower() == "on":
                oped = detect_op_ed(
                    extracted,
                    music_regions=regs,
                    seconds=int(op_ed_seconds),
                    threshold=float(music_threshold),
                )
                oped_path = analysis_dir / "op_ed.json"
                write_oped_json(oped, oped_path)
                logger.info(
                    "op_ed_detect_done",
                    path=str(oped_path),
                    seconds=int(op_ed_seconds),
                    threshold=float(music_threshold),
                )
        except Exception:
            logger.exception("music_detect_failed_continue")

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

        # Tier-Next F: optional scene-aware speaker smoothing (opt-in; default off).
        if str(speaker_smoothing).lower() == "on" and str(scene_detect).lower() != "off":
            try:
                from anime_v2.diarization.smoothing import (
                    detect_scenes_audio,
                    smooth_speakers_in_scenes,
                    write_speaker_smoothing_report,
                )

                scenes = detect_scenes_audio(Path(str(extracted)))
                utts2, changes = smooth_speakers_in_scenes(
                    utts,
                    scenes,
                    min_turn_s=float(smoothing_min_turn),
                    surround_gap_s=float(smoothing_surround_gap),
                )
                utts = utts2
                write_speaker_smoothing_report(
                    analysis_dir / "speaker_smoothing.json",
                    scenes=scenes,
                    changes=changes,
                    enabled=True,
                    config={
                        "scene_detect": str(scene_detect).lower(),
                        "min_turn_s": float(smoothing_min_turn),
                        "surround_gap_s": float(smoothing_surround_gap),
                    },
                )
                logger.info(
                    "speaker_smoothing_done",
                    scenes=len(scenes),
                    changes=len(changes),
                    path=str(analysis_dir / "speaker_smoothing.json"),
                )
            except Exception:
                logger.exception("speaker_smoothing_failed_continue")

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

        thresholds = {"sim": float(char_sim_thresh)}
        # Map diar speaker label -> persistent character id
        lab_to_char: dict[str, str] = {}
        vm_store = None
        vm_map: dict[str, str] = {}
        vm_meta: dict[str, dict[str, object]] = {}
        vm_enabled = str(voice_memory).lower() == "on"
        if vm_enabled:
            try:
                from anime_v2.voice_memory.store import VoiceMemoryStore, compute_episode_key

                vm_root = Path(voice_memory_dir or get_settings().voice_memory_dir).resolve()
                vm_store = VoiceMemoryStore(vm_root)
                if voice_character_map and voice_character_map.exists():
                    mdata = read_json(voice_character_map, default={})
                    if isinstance(mdata, dict):
                        vm_map = {
                            str(k): str(v)
                            for k, v in mdata.items()
                            if str(k).strip() and str(v).strip()
                        }
                episode_key = compute_episode_key(audio_hash=None, video_path=video)
            except Exception as ex:
                logger.warning("[v2] voice-memory unavailable; using legacy mapping (%s)", ex)
                vm_store = None
                episode_key = ""
        else:
            episode_key = ""
            store = None
            try:
                store = CharacterStore.default()
                store.load()
            except Exception:
                store = None
        for lab, segs in by_label.items():
            # pick longest seg wav for embedding
            segs_sorted = sorted(segs, key=lambda t: (t[1] - t[0]), reverse=True)
            rep_wav = segs_sorted[0][2]
            if vm_store is not None:
                try:
                    manual = vm_map.get(lab)
                    if manual:
                        cid = vm_store.ensure_character(character_id=manual)
                        sim_score = 1.0
                        provider = "manual"
                    else:
                        cid, sim_score, provider = vm_store.match_or_create_from_wav(
                            rep_wav,
                            device=chosen_device,
                            threshold=float(voice_match_threshold),
                            auto_enroll=bool(voice_auto_enroll),
                        )
                    lab_to_char[lab] = cid
                    vm_meta[lab] = {
                        "character_id": cid,
                        "similarity": float(sim_score),
                        "provider": str(provider),
                        "confidence": float(max(0.0, min(1.0, sim_score))),
                    }
                except Exception as ex:
                    logger.warning("[v2] voice-memory match failed (%s): %s", lab, ex)
                    lab_to_char[lab] = lab
            else:
                if store is None:
                    lab_to_char[lab] = lab
                    continue
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

                    emb_dir = (out_dir / "voices" / "embeddings").resolve()
                    emb_dir.mkdir(parents=True, exist_ok=True)
                    emb_path = emb_dir / f"{cid}.npy"
                    np.save(str(emb_path), emb.astype("float32"))
                    speaker_embeddings[cid] = str(emb_path)
                except Exception:
                    pass

        if store is not None:
            from contextlib import suppress

            with suppress(Exception):
                store.save()
        if vm_store is not None and episode_key:
            from contextlib import suppress

            with suppress(Exception):
                vm_store.write_episode_mapping(
                    episode_key,
                    source={"video_path": str(video), "show_id": str(show)},
                    mapping=vm_meta,
                )

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
            if project_name or style_guide_path:
                try:
                    from anime_v2.text.style_guide import (
                        apply_style_guide_to_segments,
                        load_style_guide,
                        resolve_style_guide_path,
                    )

                    sg_path = resolve_style_guide_path(
                        project=str(project_name or ""),
                        style_guide_path=Path(style_guide_path) if style_guide_path else None,
                    )
                    if sg_path and sg_path.exists():
                        guide = load_style_guide(sg_path, project=str(project_name or ""))
                        analysis_dir = out_dir / "analysis"
                        analysis_dir.mkdir(parents=True, exist_ok=True)
                        segments_for_mt = apply_style_guide_to_segments(
                            segments_for_mt,
                            guide=guide,
                            out_jsonl=(analysis_dir / "style_guide_applied.jsonl"),
                            stage="post_translate",
                            job_id=str(out_dir.name),
                        )
                except Exception:
                    logger.exception("style_guide_failed_continue")
            if str(pg_mode).lower() != "off":
                try:
                    from anime_v2.text.pg_filter import apply_pg_filter_to_segments

                    analysis_dir = out_dir / "analysis"
                    analysis_dir.mkdir(parents=True, exist_ok=True)
                    segments_for_mt, _ = apply_pg_filter_to_segments(
                        segments_for_mt,
                        pg=str(pg_mode).lower(),
                        pg_policy_path=Path(pg_policy_path).resolve() if pg_policy_path else None,
                        report_path=(analysis_dir / "pg_filter_report.json"),
                        job_id=str(out_dir.name),
                    )
                except Exception:
                    logger.exception("pg_filter_failed_continue")
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

            # Tier-Next E: optional project style guide (opt-in; OFF by default).
            if project_name or style_guide_path:
                try:
                    from anime_v2.text.style_guide import (
                        apply_style_guide_to_segments,
                        load_style_guide,
                        resolve_style_guide_path,
                    )

                    sg_path = resolve_style_guide_path(
                        project=str(project_name or ""),
                        style_guide_path=Path(style_guide_path) if style_guide_path else None,
                    )
                    if sg_path and sg_path.exists():
                        guide = load_style_guide(sg_path, project=str(project_name or ""))
                        analysis_dir = out_dir / "analysis"
                        analysis_dir.mkdir(parents=True, exist_ok=True)
                        translated_segments = apply_style_guide_to_segments(
                            translated_segments,
                            guide=guide,
                            out_jsonl=(analysis_dir / "style_guide_applied.jsonl"),
                            stage="post_translate",
                            job_id=str(out_dir.name),
                        )
                except Exception:
                    logger.exception("style_guide_failed_continue")

            # Tier-Next C: per-run PG mode (opt-in; OFF by default).
            if str(pg_mode).lower() != "off":
                try:
                    from anime_v2.text.pg_filter import apply_pg_filter_to_segments

                    analysis_dir = out_dir / "analysis"
                    analysis_dir.mkdir(parents=True, exist_ok=True)
                    translated_segments, _ = apply_pg_filter_to_segments(
                        translated_segments,
                        pg=str(pg_mode).lower(),
                        pg_policy_path=Path(pg_policy_path).resolve() if pg_policy_path else None,
                        report_path=(analysis_dir / "pg_filter_report.json"),
                        job_id=str(out_dir.name),
                    )
                except Exception:
                    logger.exception("pg_filter_failed_continue")

            # Optional timing-aware translation fit (Tier-1 B).
            if timing_fit:
                try:
                    from anime_v2.timing.fit_text import fit_translation_to_time

                    for seg in translated_segments:
                        try:
                            tgt_s = max(0.0, float(seg["end"]) - float(seg["start"]))
                            pre = str(seg.get("text") or "")
                            fitted, stats = fit_translation_to_time(
                                pre,
                                tgt_s,
                                tolerance=float(tolerance),
                                wps=float(wps),
                                max_passes=4,
                            )
                            seg["text_pre_fit"] = pre
                            seg["text"] = fitted
                            seg["timing_fit"] = stats.to_dict()
                        except Exception:
                            continue
                except Exception as ex:
                    logger.warning("[v2] timing-fit skipped (%s)", ex)

            write_json(
                translated_json,
                {"src_lang": src_lang, "tgt_lang": tgt_lang, "segments": translated_segments},
            )

            # Convert to SRT lines (speaker preserved; text from translated)
            subs_use_fit = bool(get_settings().subs_use_fitted_text) if timing_fit else True
            srt_lines = [
                {
                    "start": s["start"],
                    "end": s["end"],
                    "speaker_id": s["speaker"],
                    "text": (
                        s.get("text") if subs_use_fit else (s.get("text_pre_fit") or s.get("text"))
                    ),
                }
                for s in translated_segments
            ]
            _write_srt_from_lines(srt_lines, translated_srt)
            subs_srt_path = translated_srt
            if subs_choice in {"tgt", "both"} and subs_fmt in {"vtt", "both"}:
                try:
                    _write_vtt_from_lines(
                        [
                            {
                                "start": s["start"],
                                "end": s["end"],
                                "text": (
                                    s.get("text")
                                    if subs_use_fit
                                    else (s.get("text_pre_fit") or s.get("text"))
                                ),
                            }
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
            voice_memory=(str(voice_memory).lower() == "on"),
            voice_memory_dir=Path(voice_memory_dir).resolve() if voice_memory_dir else None,
            voice_match_threshold=float(voice_match_threshold),
            voice_auto_enroll=bool(voice_auto_enroll),
            voice_character_map=(
                Path(voice_character_map).resolve() if voice_character_map else None
            ),
            tts_provider=tts_provider,
            emotion_mode=emotion_mode,
            expressive=expressive_mode,
            expressive_strength=float(expressive_strength),
            expressive_debug=bool(expressive_debug),
            source_audio_wav=Path(str(extracted)) if extracted is not None else None,
            music_regions_path=Path(music_regions_path).resolve() if music_regions_path else None,
            director=(str(director_mode).lower() == "on"),
            director_strength=float(director_strength),
            speech_rate=float(speech_rate),
            pitch=float(pitch),
            energy=float(energy),
            pacing=bool(pacing),
            pacing_min_ratio=float(pacing_min_stretch),
            pacing_max_ratio=float(pacing_max_stretch),
            timing_tolerance=float(tolerance),
            timing_debug=bool(timing_debug),
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
            from anime_v2.stages.export import export_hls, export_m4a, export_mkv, export_mkv_multitrack, export_mp4

            bg = background_wav or Path(str(extracted))
            # If separation enabled and music regions exist, preserve original audio during music
            if background_wav is not None and music_regions_path is not None:
                try:
                    from anime_v2.audio.music_detect import build_music_preserving_bed
                    from anime_v2.utils.io import read_json as _rj

                    data = _rj(Path(music_regions_path), default={})
                    regs = data.get("regions", []) if isinstance(data, dict) else []
                    if isinstance(regs, list) and regs:
                        bed = audio_dir / "background_music_preserve.wav"
                        bg = build_music_preserving_bed(
                            background_wav=background_wav,
                            original_wav=Path(str(extracted)),
                            regions=[r for r in regs if isinstance(r, dict)],
                            out_wav=bed,
                        )
                except Exception:
                    logger.exception("music_bed_build_failed_continue")
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
            # Multi-track output (opt-in): write track artifacts + mux multi-audio MKV (preferred).
            if str(multitrack).lower() == "on":
                from anime_v2.audio.tracks import build_multitrack_artifacts

                tracks = build_multitrack_artifacts(
                    job_dir=out_dir,
                    original_wav=Path(str(extracted)),
                    dubbed_wav=final_mix,
                    dialogue_wav=tts_wav,
                    background_wav=bg if background_wav is not None else None,
                )
                if str(container).lower() == "mkv" and "mkv" in emit_set:
                    outs["mkv"] = export_mkv_multitrack(
                        video_in=video,
                        tracks=[
                            {
                                "path": str(tracks.original_full_wav),
                                "title": "Original (JP)",
                                "language": "jpn",
                                "default": "0",
                            },
                            {
                                "path": str(tracks.dubbed_full_wav),
                                "title": "Dubbed (EN)",
                                "language": "eng",
                                "default": "1",
                            },
                            {
                                "path": str(tracks.background_only_wav),
                                "title": "Background Only",
                                "language": "und",
                                "default": "0",
                            },
                            {
                                "path": str(tracks.dialogue_only_wav),
                                "title": "Dialogue Only",
                                "language": "eng",
                                "default": "0",
                            },
                        ],
                        srt=None if no_subs else subs_srt_path,
                        out_path=out_dir / "dub.mkv",
                    )
                elif str(container).lower() == "mp4":
                    # MP4 fallback: keep normal MP4 output and write sidecar audio tracks.
                    sidecar_dir = out_dir / "audio" / "tracks"
                    export_m4a(tracks.original_full_wav, sidecar_dir / "original_full.m4a", title="Original (JP)", language="jpn")
                    export_m4a(tracks.background_only_wav, sidecar_dir / "background_only.m4a", title="Background Only", language="und")
                    export_m4a(tracks.dialogue_only_wav, sidecar_dir / "dialogue_only.m4a", title="Dialogue Only", language="eng")
                    export_m4a(tracks.dubbed_full_wav, sidecar_dir / "dubbed_full.m4a", title="Dubbed (EN)", language="eng")

            if "mkv" in emit_set and "mkv" not in outs:
                outs["mkv"] = export_mkv(video, final_mix, None if no_subs else subs_srt_path, out_dir / "dub.mkv")
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
            if str(multitrack).lower() == "on":
                from anime_v2.audio.tracks import build_multitrack_artifacts
                from anime_v2.stages.export import export_m4a, export_mkv_multitrack

                mixed_wav = outs.get("mixed_wav", None)
                if mixed_wav is not None and Path(mixed_wav).exists():
                    stems_bg = (out_dir / "stems" / "background.wav") if (out_dir / "stems" / "background.wav").exists() else None
                    tracks = build_multitrack_artifacts(
                        job_dir=out_dir,
                        original_wav=Path(str(extracted)),
                        dubbed_wav=Path(mixed_wav),
                        dialogue_wav=tts_wav,
                        background_wav=stems_bg,
                    )
                    if str(container).lower() == "mkv" and "mkv" in emit_set:
                        outs["mkv"] = export_mkv_multitrack(
                            video_in=video,
                            tracks=[
                                {"path": str(tracks.original_full_wav), "title": "Original (JP)", "language": "jpn", "default": "0"},
                                {"path": str(tracks.dubbed_full_wav), "title": "Dubbed (EN)", "language": "eng", "default": "1"},
                                {"path": str(tracks.background_only_wav), "title": "Background Only", "language": "und", "default": "0"},
                                {"path": str(tracks.dialogue_only_wav), "title": "Dialogue Only", "language": "eng", "default": "0"},
                            ],
                            srt=None if no_subs else subs_srt_path,
                            out_path=out_dir / "dub.mkv",
                        )
                    elif str(container).lower() == "mp4":
                        sidecar_dir = out_dir / "audio" / "tracks"
                        export_m4a(tracks.original_full_wav, sidecar_dir / "original_full.m4a", title="Original (JP)", language="jpn")
                        export_m4a(tracks.background_only_wav, sidecar_dir / "background_only.m4a", title="Background Only", language="und")
                        export_m4a(tracks.dialogue_only_wav, sidecar_dir / "dialogue_only.m4a", title="Dialogue Only", language="eng")
                        export_m4a(tracks.dubbed_full_wav, sidecar_dir / "dubbed_full.m4a", title="Dubbed (EN)", language="eng")
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

    # Tier-3A: optional lip-sync plugin (default off)
    try:
        mode = str(lipsync_mode or get_settings().lipsync or "off").strip().lower()
        if mode != "off":
            from anime_v2.plugins.lipsync.base import LipSyncRequest
            from anime_v2.plugins.lipsync.registry import resolve_lipsync_plugin
            from anime_v2.plugins.lipsync.wav2lip_plugin import _parse_bbox

            plugin = resolve_lipsync_plugin(
                mode, wav2lip_dir=wav2lip_dir, wav2lip_checkpoint=wav2lip_checkpoint
            )
            if plugin is None or not plugin.is_available():
                msg = (
                    "Lip-sync plugin requested but unavailable. "
                    "Setup: place Wav2Lip at third_party/wav2lip and set WAV2LIP_CHECKPOINT "
                    "(or pass --wav2lip-dir/--wav2lip-checkpoint)."
                )
                if bool(strict_plugins) or bool(get_settings().strict_plugins):
                    raise RuntimeError(msg)
                logger.warning("[v2] %s", msg)
            else:
                tmp_dir = out_dir / "tmp" / "lipsync"
                out_lip = out_dir / "final_lipsynced.mp4"
                audio_for_lip = (
                    (audio_dir / "final_mix.wav")
                    if (audio_dir / "final_mix.wav").exists()
                    else tts_wav
                )
                bbox = (
                    _parse_bbox(str(lipsync_box or ""))
                    if str(lipsync_face or "").lower() == "bbox"
                    else None
                )
                req = LipSyncRequest(
                    input_video=video,
                    dubbed_audio_wav=audio_for_lip,
                    output_video=out_lip,
                    work_dir=tmp_dir,
                    face_mode=str(lipsync_face or "auto").lower(),
                    device=str(lipsync_device or "auto").lower(),
                    bbox=bbox,
                    timeout_s=int(get_settings().lipsync_timeout_s),
                )
                outp = plugin.run(req)
                logger.info("[v2] lipsync: ok → %s", outp)
    except Exception as ex:
        if bool(strict_plugins) or bool(get_settings().strict_plugins):
            raise
        logger.warning("[v2] lipsync skipped (%s)", ex)

    logger.info("[v2] Done. Output: %s", dub_mkv)

    # Tier-Next D: optional QA scoring (offline-only; writes reports, does not change outputs)
    if str(qa_mode).lower() == "on":
        try:
            from anime_v2.qa.scoring import score_job

            score_job(out_dir, enabled=True, write_outputs=True)
        except Exception:
            logger.exception("qa_failed_continue")

# Public entrypoint (project.scripts -> anime_v2.cli:cli)
from anime_v2.review.cli import review as review  # noqa: E402
from anime_v2.qa.cli import qa as qa  # noqa: E402

cli = DefaultGroup(name="anime-v2", help="anime-v2 CLI (run + review)")  # type: ignore[assignment]
cli.add_command(run)
cli.add_command(review)
cli.add_command(qa)


if __name__ == "__main__":  # pragma: no cover
    cli()
