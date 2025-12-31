from __future__ import annotations

from pathlib import Path

from anime_v2.utils.log import logger


def run(video: Path, ckpt_dir: Path, out_dir: Path, **_) -> Path:
    """
    Mux video + dubbed audio into a final container.

    Stub for pipeline-v2; replace with actual implementation.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{video.stem}_dubbed.mkv"
    logger.info("[v2] mkv_export.run(video=%s) -> %s", video, out)
    return out

