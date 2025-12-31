from __future__ import annotations

from pathlib import Path

from anime_v2.utils.config import get_settings
from anime_v2.utils.log import logger


def run(transcript_json: Path, ckpt_dir: Path, **_) -> Path:
    """
    Text-to-speech stage.

    Stub for pipeline-v2; replace with actual implementation.
    """
    settings = get_settings()
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    dubbed = ckpt_dir / "dubbed.wav"
    logger.info(
        "[v2] tts.run(transcript=%s, model=%s, lang=%s, speaker=%s) -> %s",
        transcript_json,
        settings.tts_model,
        settings.tts_lang,
        settings.tts_speaker,
        dubbed,
    )
    return dubbed

