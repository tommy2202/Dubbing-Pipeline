import pathlib
import subprocess

from anime_v1.utils import logger


def run(input_str: str, ckpt_dir: pathlib.Path) -> pathlib.Path:
    """Return a local video path. If input is a URL, download via yt-dlp."""
    p = pathlib.Path(input_str)
    if p.exists():
        logger.info("Using local video: %s", p)
        return p
    # Treat as URL
    out = ckpt_dir / "input.mp4"
    try:
        logger.info("Downloading video via yt-dlp → %s", out)
        cmd = [
            "yt-dlp",
            "-f",
            "mp4/bestvideo+bestaudio",
            "-o",
            str(out),
            input_str,
        ]
        subprocess.run(cmd, check=True)
        return out
    except Exception as ex:  # pragma: no cover
        logger.error("yt-dlp failed to download input (%s)", ex)
        raise
