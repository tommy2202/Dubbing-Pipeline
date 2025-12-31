from __future__ import annotations

from pathlib import Path

from anime_v2.utils.log import logger


def run(
    video: Path,
    ckpt_dir: Path,
    out_dir: Path,
    *,
    dubbed_audio: Path | None = None,
    mkv_out: Path | None = None,
    **_,
) -> Path:
    """
    Mux video + dubbed audio into a final container.

    Stub: does not actually mux yet; just ensures output path exists.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out = mkv_out or (out_dir / "dub.mkv")
    logger.info("[v2] mkv_export.run(video=%s, dubbed_audio=%s) -> %s", video, dubbed_audio, out)
    out.write_bytes(b"")
    return out

