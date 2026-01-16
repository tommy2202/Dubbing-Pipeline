import pathlib
import time

from dubbing_pipeline_legacy.utils import checkpoints, logger

_model_cache: dict[str, object] = {}


def _load(model_size: str):
    model_size = (model_size or "small").lower()
    if model_size in _model_cache:
        return _model_cache[model_size]
    try:
        import whisper  # type: ignore
    except Exception as ex:
        raise RuntimeError(
            "Whisper is not installed. Install `openai-whisper` (or use the primary pipeline) "
            "or rely on the Vosk fallback if configured."
        ) from ex
    try:
        logger.info("Loading Whisper-%s …", model_size)
        _model_cache[model_size] = whisper.load_model(model_size)
        return _model_cache[model_size]
    except Exception as ex:
        logger.warning("Failed to load Whisper-%s (%s); falling back to tiny.", model_size, ex)
        if "tiny" not in _model_cache:
            _model_cache["tiny"] = whisper.load_model("tiny")
        return _model_cache["tiny"]


def _try_vosk_transcribe(audio_wav: pathlib.Path, lang: str | None):
    try:
        import json as _json
        import wave

        import vosk  # type: ignore

        wf = wave.open(str(audio_wav), "rb")
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            logger.warning("Vosk expects mono 16-bit PCM; got different format.")
        model = vosk.Model(lang=lang or "en-us")
        rec = vosk.KaldiRecognizer(model, wf.getframerate())
        segments = []
        while True:
            data = wf.readframes(4000)
            if len(data) == 0:
                break
            if rec.AcceptWaveform(data):
                r = _json.loads(rec.Result())
                if r.get("text"):
                    segments.append({"start": 0.0, "end": 0.0, "text": r["text"]})
        r = _json.loads(rec.FinalResult())
        if r.get("text"):
            segments.append({"start": 0.0, "end": 0.0, "text": r["text"]})
        return {"text": " ".join(seg["text"] for seg in segments), "segments": segments}
    except Exception as ex:  # pragma: no cover
        logger.warning("Vosk fallback failed (%s)", ex)
        return None


def run(
    audio_wav: pathlib.Path,
    ckpt_dir: pathlib.Path,
    *,
    model_size: str = "small",
    prefer_translate: bool = True,
    src_lang: str | None = None,
    tgt_lang: str = "en",
):
    out = ckpt_dir / "transcript.json"
    if out.exists():
        logger.info("Transcript exists, skip.")
        return out

    t0 = time.time()
    try:
        m = _load(model_size)
        # Whisper translate task reliably targets English only.
        do_translate = prefer_translate and (tgt_lang.lower() == "en")
        task = "translate" if do_translate else "transcribe"
        kwargs = {}
        if src_lang:
            kwargs["language"] = src_lang
        res = m.transcribe(str(audio_wav), task=task, **kwargs)
        logger.info("Whisper %s finished in %.1fs", task, time.time() - t0)
    except Exception as ex:
        logger.warning("Whisper failed (%s); trying Vosk fallback", ex)
        res = _try_vosk_transcribe(audio_wav, src_lang)
        if res is None:
            raise
    checkpoints.save(res, out)
    return out
