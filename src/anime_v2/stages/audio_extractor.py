from __future__ import annotations

import subprocess
from pathlib import Path

from anime_v2.utils.log import logger


def run(video: Path, ckpt_dir: Path, wav_out: Path | None = None, **_) -> Path:
    """
    Extract mono 16kHz WAV from video.

    Uses ffmpeg.
    """
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    wav = wav_out or (ckpt_dir / "audio.wav")
    logger.info("[v2] Extracting audio → %s", wav)

    if wav.exists():
        logger.info("[v2] Audio already extracted")
        return wav

    wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video), "-ac", "1", "-ar", "16000", str(wav)],
        check=True,
    )
    return wav


# Alias for orchestrator naming
def extract(video: Path, out_dir: Path, *, wav_out: Path | None = None) -> Path:
    return run(video=video, ckpt_dir=out_dir, wav_out=wav_out)

