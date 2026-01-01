import pathlib

import click

from anime_v1.stages import (
    audio_extractor,
    diarisation,
    downloader,
    mkv_export,
    separation,
    transcription,
    tts,
)
from anime_v1.utils import logger


def _resolve_defaults(mode: str, lipsync: bool | None, keep_bg: bool | None, voice: str) -> dict:
    # Mode-driven defaults with user override capability
    mode = mode.lower()
    resolved = {
        "asr_model": "small",
        "prefer_translate": True,  # let ASR directly produce target text
        "lipsync": False,
        "keep_bg": True,
        "tts_preference": "default",  # default | clone
    }
    if mode == "high":
        resolved.update(
            {
                "asr_model": "large-v2",
                "prefer_translate": False,  # do pure ASR, translate in a separate stage (if available)
                "lipsync": True,
                "keep_bg": True,
                "tts_preference": "clone" if voice in ("auto", "clone") else "default",
            }
        )
    elif mode == "medium":
        resolved.update(
            {
                "asr_model": "small",
                "prefer_translate": True,
                "lipsync": False,
                "keep_bg": True,
                "tts_preference": "default",
            }
        )
    else:  # low
        resolved.update(
            {
                "asr_model": "tiny",
                "prefer_translate": True,
                "lipsync": False,
                "keep_bg": False,
                "tts_preference": "default",
            }
        )

    if lipsync is not None:
        resolved["lipsync"] = lipsync
    if keep_bg is not None:
        resolved["keep_bg"] = keep_bg
    if voice in ("default", "clone"):
        resolved["tts_preference"] = voice
    return resolved


@click.command()
@click.argument("video", type=str)
@click.option(
    "--src-lang", default=None, help="Source language code (e.g. ja). Autodetect if omitted."
)
@click.option("--tgt-lang", default="en", show_default=True, help="Target language code (e.g. en)")
@click.option(
    "--mode",
    type=click.Choice(["high", "medium", "low"], case_sensitive=False),
    default="medium",
    show_default=True,
)
@click.option(
    "--voice",
    type=click.Choice(["auto", "default", "clone"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="Voice preference (clone tries XTTS/Tortoise)",
)
@click.option(
    "--out-dir",
    type=click.Path(file_okay=False),
    default="/data/out",
    show_default=True,
    help="Output directory for final file",
)
@click.option(
    "--lipsync",
    "lipsync_flag",
    flag_value=True,
    default=None,
    help="Enable lip-sync stage if available",
)
@click.option("--no-lipsync", "lipsync_flag", flag_value=False, help="Disable lip-sync stage")
@click.option(
    "--keep-bg",
    "keep_bg_flag",
    flag_value=True,
    default=None,
    help="Mix original background audio under dub",
)
@click.option(
    "--no-keep-bg", "keep_bg_flag", flag_value=False, help="Do not mix original background audio"
)
def cli(video, src_lang, tgt_lang, mode, voice, out_dir, lipsync_flag, keep_bg_flag):
    """Run the dubbing pipeline on VIDEO.

    Example: anime-v1 <video.mp4> --src-lang ja --tgt-lang en --mode high
    """
    # Accept a URL or a local path
    video_pathlike = pathlib.Path(video)
    safe_stem = video_pathlike.stem if video_pathlike.exists() else "remote"
    ckpt = pathlib.Path("checkpoints") / safe_stem
    ckpt.mkdir(parents=True, exist_ok=True)
    video = downloader.run(video, ckpt_dir=ckpt)

    defaults = _resolve_defaults(mode, lipsync_flag, keep_bg_flag, voice)
    logger.info(
        "Settings → mode=%s asr_model=%s prefer_translate=%s lipsync=%s keep_bg=%s tts=%s",
        mode,
        defaults["asr_model"],
        defaults["prefer_translate"],
        defaults["lipsync"],
        defaults["keep_bg"],
        defaults["tts_preference"],
    )

    # 1) Audio extraction
    wav = audio_extractor.run(video, ckpt_dir=ckpt)

    # 2) (Optional) diarisation placeholder
    diarisation.run(wav, ckpt_dir=ckpt)

    # 3) Transcription (ASR), possibly direct translate if prefer_translate
    transcript_src_or_tgt = transcription.run(
        wav,
        ckpt_dir=ckpt,
        model_size=defaults["asr_model"],
        prefer_translate=defaults["prefer_translate"],
        src_lang=src_lang,
        tgt_lang=tgt_lang,
    )

    # 4) Optional explicit translation stage for high mode
    #    If ASR already translated, this returns the same path.
    try:
        from anime_v1.stages import translation

        transcript_for_tts = translation.run(
            transcript_src_or_tgt,
            ckpt_dir=ckpt,
            src_lang=src_lang,
            tgt_lang=tgt_lang,
            enabled=(mode.lower() == "high" and defaults["prefer_translate"] is False),
        )
    except Exception as ex:  # pragma: no cover - optional dep may be missing
        logger.warning("Translation stage skipped (%s)", ex)
        transcript_for_tts = transcript_src_or_tgt

    # 5) TTS with alignment
    dubbed_wav = tts.run(
        transcript_for_tts,
        ckpt_dir=ckpt,
        tgt_lang=tgt_lang,
        preference=defaults["tts_preference"],
        source_audio=wav,
    )

    # 5.5) Optional source separation for background preservation
    if defaults["keep_bg"]:
        try:
            separation.run(wav, ckpt_dir=ckpt)
        except Exception as ex:  # pragma: no cover
            logger.warning("Separation stage skipped (%s)", ex)

    # 6) Optional lip-sync stage (stubbed by default)
    video_for_mux = video
    if defaults["lipsync"]:
        try:
            from anime_v1.stages import lipsync

            lip_vid = lipsync.run(video=video, audio=dubbed_wav, ckpt_dir=ckpt)
            if lip_vid is not None:
                video_for_mux = lip_vid
        except Exception as ex:  # pragma: no cover
            logger.warning("Lip-sync stage skipped (%s)", ex)

    # 7) Export/mux
    result = mkv_export.run(
        video_for_mux,
        ckpt_dir=ckpt,
        out_dir=pathlib.Path(out_dir),
        keep_bg=defaults["keep_bg"],
        transcript_override=pathlib.Path(transcript_for_tts),
    )
    # Emit final summary
    logger.info(
        "Summary: mode=%s lipsync=%s keep_bg=%s asr=%s tts=%s output=%s",
        mode,
        defaults["lipsync"],
        defaults["keep_bg"],
        defaults["asr_model"],
        defaults["tts_preference"],
        result,
    )


if __name__ == "__main__":
    cli()
