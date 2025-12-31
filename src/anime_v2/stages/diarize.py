from __future__ import annotations

from pathlib import Path

from anime_v2.utils.log import logger


def run(wav: Path, ckpt_dir: Path, **_) -> Path:
    """
    Speaker diarization stage.

    Stub for pipeline-v2; replace with actual implementation.
    """
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    diar = ckpt_dir / "diarization.json"
    logger.info("[v2] diarize.run(wav=%s) -> %s", wav, diar)
    return diar

