import subprocess, pathlib
from anime_v1.utils import logger
def run(video: pathlib.Path, ckpt_dir: pathlib.Path, **_):
    wav = ckpt_dir / 'audio.wav'
    if wav.exists():
        logger.info('Audio already extracted')
        return wav
    logger.info('Extracting audio → %s', wav)
    wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['ffmpeg','-y','-i',str(video),'-ac','1','-ar','16000',str(wav)], check=True)
    return wav
