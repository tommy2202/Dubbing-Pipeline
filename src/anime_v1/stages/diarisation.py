import pathlib, time
from anime_v1.utils import logger, checkpoints

def run(audio_wav: pathlib.Path, ckpt_dir: pathlib.Path, **_):
    out = ckpt_dir / "speaker_segments.json"
    if out.exists():
        logger.info("Diarisation exists, skip.")
        return out
    logger.info("Running placeholder diarisation (labels everything Speaker_1)")
    meta = {"segments": [{"speaker": "Speaker_1", "start": 0.0, "end": 0.0}]}
    checkpoints.save(meta, out)
    return out
