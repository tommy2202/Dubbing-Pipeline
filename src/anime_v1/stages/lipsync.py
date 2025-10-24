import pathlib
from anime_v1.utils import logger


def run(video: pathlib.Path, audio: pathlib.Path, ckpt_dir: pathlib.Path):
    """Lip-sync stage placeholder.

    If a lip-sync backend (e.g., Wav2Lip) is available, this function should
    generate a new video aligned to `audio` and return its path. In this stub,
    we simply log and return None to indicate no change.
    """
    logger.info("Lip-sync stage not configured; skipping.")
    return None
