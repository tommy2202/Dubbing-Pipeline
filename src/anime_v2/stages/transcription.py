from __future__ import annotations

from pathlib import Path

from anime_v2.utils.config import get_settings
from anime_v2.utils.log import logger


def run(wav: Path, ckpt_dir: Path, **_) -> Path:
    """
    ASR (Whisper) stage.

    Stub for pipeline-v2; replace with actual implementation.
    """
    settings = get_settings()
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    transcript = ckpt_dir / "transcript.json"
    logger.info(
        "[v2] transcription.run(wav=%s, model=%s) -> %s",
        wav,
        settings.whisper_model,
        transcript,
    )
    return transcript

