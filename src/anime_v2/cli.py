from __future__ import annotations

import time
from pathlib import Path

import click

from anime_v2.stages import audio_extractor, mkv_export, tts
from anime_v2.stages.diarize import run as diarize_run
from anime_v2.stages.transcription import transcribe
from anime_v2.utils.io import write_json
from anime_v2.utils.log import logger
from anime_v2.utils.paths import output_dir_for


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
def cli(video: Path, device: str, mode: str, src_lang: str, tgt_lang: str, no_translate: bool) -> None:
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

    task = "transcribe" if no_translate else "translate"

    # Output layout requirement:
    # Output/<video_stem>/{wav,srt,tts.wav,dub.mkv}
    stem = video.stem
    out_dir = output_dir_for(video)
    out_dir.mkdir(parents=True, exist_ok=True)

    wav_path = out_dir / "audio.wav"
    srt_out = out_dir / f"{stem}.srt"
    diar_json = out_dir / "diarization.json"
    tts_wav = out_dir / "tts.wav"
    dub_mkv = out_dir / "dub.mkv"

    logger.info(
        "[v2] Starting dub: video=%s mode=%s model=%s device=%s task=%s src_lang=%s tgt_lang=%s",
        video,
        mode,
        chosen_model,
        chosen_device,
        task,
        src_lang,
        tgt_lang,
    )

    t0 = time.perf_counter()

    # 1) Extract audio
    extracted = audio_extractor.run(video=video, ckpt_dir=out_dir, wav_out=wav_path)

    # 2) Diarization (stable speaker IDs)
    try:
        segments, speaker_embeddings = diarize_run(audio_path=extracted, out_dir=out_dir)
        write_json(
            diar_json,
            {
                "audio_path": str(extracted),
                "segments": segments,
                "speaker_embeddings": speaker_embeddings,
            },
        )
        logger.info(
            "[v2] diarization.json written (%s segments, %s stable speakers) → %s",
            len(segments),
            len(set(s["speaker_id"] for s in segments)),
            diar_json,
        )
    except Exception as ex:
        logger.warning("[v2] Diarization failed/skipped: %s", ex)

    # 3) Whisper transcription/translation -> SRT
    transcribe(
        audio_path=extracted,
        srt_out=srt_out,
        device=chosen_device,
        model_name=chosen_model,
        task=task,
        src_lang=src_lang,
        tgt_lang=tgt_lang,
    )

    # 4) TTS + mux (stubs for now, but keep file layout stable)
    try:
        tts.run(transcript_srt=srt_out, wav_out=tts_wav, ckpt_dir=out_dir)
        mkv_export.run(video=video, dubbed_audio=tts_wav, mkv_out=dub_mkv, ckpt_dir=out_dir, out_dir=out_dir)
    except Exception as ex:
        # Keep CLI usable even if downstream stages are not implemented yet.
        logger.warning("[v2] Downstream stages skipped/failed: %s", ex)

    logger.info("[v2] Done in %.2fs → %s", time.perf_counter() - t0, out_dir)


if __name__ == "__main__":  # pragma: no cover
    cli()
