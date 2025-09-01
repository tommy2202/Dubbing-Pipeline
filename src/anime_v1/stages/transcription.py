import pathlib, time
from anime_v1.utils import logger, checkpoints
import whisper
_model = None
def _load():
    global _model
    if _model is None:
        logger.info('Loading Whisper-small (translate)…')
        _model = whisper.load_model('small')
    return _model
def run(audio_wav: pathlib.Path, ckpt_dir: pathlib.Path, **_):
    out = ckpt_dir/'transcript.json'
    if out.exists():
        logger.info('Transcript exists, skip.')
        return out
    m = _load()
    t0 = time.time()
    res = m.transcribe(str(audio_wav), task='translate', language='ja')
    logger.info('Whisper finished in %.1fs', time.time()-t0)
    checkpoints.save(res, out)
    return out
