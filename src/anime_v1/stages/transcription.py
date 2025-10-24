import pathlib
import time
from typing import Optional, Dict
from anime_v1.utils import logger, checkpoints
import whisper

_model_cache: Dict[str, object] = {}


def _load(model_size: str):
    model_size = (model_size or "small").lower()
    if model_size in _model_cache:
        return _model_cache[model_size]
    try:
        logger.info("Loading Whisper-%s …", model_size)
        _model_cache[model_size] = whisper.load_model(model_size)
        return _model_cache[model_size]
    except Exception as ex:
        logger.warning("Failed to load Whisper-%s (%s); falling back to tiny.", model_size, ex)
        if "tiny" not in _model_cache:
            _model_cache["tiny"] = whisper.load_model("tiny")
        return _model_cache["tiny"]


def run(
    audio_wav: pathlib.Path,
    ckpt_dir: pathlib.Path,
    *,
    model_size: str = "small",
    prefer_translate: bool = True,
    src_lang: Optional[str] = None,
    tgt_lang: str = "en",
):
    out = ckpt_dir / "transcript.json"
    if out.exists():
        logger.info("Transcript exists, skip.")
        return out

    m = _load(model_size)
    t0 = time.time()

    # Whisper translate task reliably targets English only.
    do_translate = prefer_translate and (tgt_lang.lower() == "en")
    task = "translate" if do_translate else "transcribe"

    kwargs = {}
    if src_lang:
        kwargs["language"] = src_lang

    res = m.transcribe(str(audio_wav), task=task, **kwargs)
    logger.info("Whisper %s finished in %.1fs", task, time.time() - t0)
    checkpoints.save(res, out)
    return out
