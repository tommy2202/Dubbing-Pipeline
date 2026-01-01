import pathlib
import subprocess
import sys

from anime_v1.utils import logger


def _find_models_base() -> pathlib.Path:
    # Default location where scripts/download_models.py stores models & repo
    try:
        from config.settings import get_settings

        s = get_settings()
        base = pathlib.Path(str(getattr(s, "models_dir", "/models"))) / "Wav2Lip"
        return base
    except Exception:
        return pathlib.Path("/models/Wav2Lip")


def run(video: pathlib.Path, audio: pathlib.Path, ckpt_dir: pathlib.Path):
    """Run Wav2Lip if available; else return None.

    This implementation shells out to the Wav2Lip repo's inference script
    if present under /models/Wav2Lip. Otherwise, logs and skips.
    """
    repo = _find_models_base()
    try:
        from config.settings import get_settings

        s = get_settings()
        ckpt = pathlib.Path(str(getattr(s, "models_dir", "/models"))) / "wav2lip" / "wav2lip.pth"
    except Exception:
        ckpt = pathlib.Path("/models/wav2lip/wav2lip.pth")
    if not repo.exists() or not ckpt.exists():
        logger.info("Wav2Lip not available; skipping.")
        return None

    out = ckpt_dir / "lipsynced.mp4"
    try:
        logger.info("Running Wav2Lip for lip-sync …")
        cmd = [
            sys.executable,
            str(repo / "infer.py"),
            "--checkpoint_path",
            str(ckpt),
            "--face",
            str(video),
            "--audio",
            str(audio),
            "--outfile",
            str(out),
        ]
        subprocess.run(cmd, check=True)
        if out.exists():
            logger.info("Lip-sync complete → %s", out)
            return out
        logger.warning("Wav2Lip finished without output; skipping.")
    except Exception as ex:  # pragma: no cover
        logger.warning("Wav2Lip failed (%s)", ex)
    return None
