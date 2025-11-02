import pathlib
import subprocess
from anime_v1.utils import logger


def run(audio_wav: pathlib.Path, ckpt_dir: pathlib.Path):
    """Optional background separation using Demucs if installed.

    Produces ckpt_dir/background.wav if successful. If demucs is not
    available or processing fails, logs and returns silently.
    """
    bg = ckpt_dir / "background.wav"
    if bg.exists():
        logger.info("Background track exists, skip separation.")
        return bg
    # Check demucs availability by trying to run it
    try:
        # Demucs writes to a directory; we capture instrumental stem
        out_dir = ckpt_dir / "demucs_out"
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            "python", "-m", "demucs.separate",
            "-n", "htdemucs",
            "-o", str(out_dir),
            str(audio_wav),
        ]
        subprocess.run(cmd, check=True)
        # Prefer 'no_vocals'/'instrumental' stems if present
        candidates = []
        for p in out_dir.rglob("*.wav"):
            name = p.name.lower()
            if "no_vocals" in name or "instrumental" in name or ("vocals" not in name):
                candidates.append(p)
        if candidates:
            # Choose the longest candidate as background
            pick = max(candidates, key=lambda x: x.stat().st_size)
            logger.info("Using separated background: %s", pick)
            pick.replace(bg)
            return bg
        logger.warning("Demucs finished but could not locate background stem.")
    except Exception as ex:  # pragma: no cover
        logger.warning("Demucs not available or failed (%s)", ex)
    return None
