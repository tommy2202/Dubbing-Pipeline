from __future__ import annotations

from contextlib import suppress
from pathlib import Path

from anime_v2.jobs.checkpoint import read_ckpt, stage_is_done, write_ckpt
from anime_v2.utils.ffmpeg_safe import extract_audio_mono_16k
from anime_v2.utils.log import logger


def run(
    video: Path, ckpt_dir: Path, wav_out: Path | None = None, *, job_id: str | None = None, **_
) -> Path:
    """
    Extract mono 16kHz WAV from video.

    Uses ffmpeg.
    """
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    wav = wav_out or (ckpt_dir / "audio.wav")
    logger.info("[v2] Extracting audio → %s", wav)

    ckpt_path = ckpt_dir / ".checkpoint.json"
    if job_id:
        ckpt = read_ckpt(job_id, ckpt_path=ckpt_path)
        if wav.exists() and stage_is_done(ckpt, "audio"):
            logger.info("[v2] audio stage checkpoint hit")
            return wav

    if wav.exists():
        logger.info("[v2] Audio already extracted")
        if job_id:
            with suppress(Exception):
                write_ckpt(
                    job_id,
                    "audio",
                    {"audio_wav": wav},
                    {"work_dir": str(ckpt_dir)},
                    ckpt_path=ckpt_path,
                )
        return wav

    wav.parent.mkdir(parents=True, exist_ok=True)
    extract_audio_mono_16k(src=video, dst=wav, timeout_s=120)
    if job_id:
        with suppress(Exception):
            write_ckpt(
                job_id,
                "audio",
                {"audio_wav": wav},
                {"work_dir": str(ckpt_dir)},
                ckpt_path=ckpt_path,
            )
    return wav


# Alias for orchestrator naming
def extract(
    video: Path,
    out_dir: Path,
    *,
    wav_out: Path | None = None,
    job_id: str | None = None,
    **_: object,
) -> Path:
    # Accept job_id/**_ for forward-compat with orchestrators passing extra kwargs.
    return run(video=video, ckpt_dir=out_dir, wav_out=wav_out, job_id=job_id)
