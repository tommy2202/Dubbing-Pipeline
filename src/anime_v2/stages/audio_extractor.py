from __future__ import annotations

from pathlib import Path

from anime_v2.utils.log import logger


def run(video: Path, ckpt_dir: Path, **_) -> Path:
    """
    Extract mono 16kHz WAV from video.

    Stub for pipeline-v2; replace with actual implementation.
    """
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    wav = ckpt_dir / "audio.wav"
    logger.info("[v2] audio_extractor.run(video=%s) -> %s", video, wav)
    # TODO: call ffmpeg here (see anime_v1 implementation)
    return wav

