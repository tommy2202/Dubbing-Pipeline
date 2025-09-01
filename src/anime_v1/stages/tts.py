import pathlib, json, tempfile
from anime_v1.utils import logger, checkpoints
from TTS.api import TTS
from pydub import AudioSegment

SAMPLE_RATE = 22050
tts_model = None

def _load():
    global tts_model
    if tts_model is None:
        logger.info("Loading Coqui TTS en/vctk/vits …")
        tts_model = TTS(model_name="tts_models/en/vctk/vits", progress_bar=False, gpu=False)
        # stash the first speaker as default
        tts_model.default_speaker = tts_model.speakers[0]
    return tts_model

def run(transcript_json: pathlib.Path, ckpt_dir: pathlib.Path, **_):
    out = ckpt_dir / "dubbed.wav"
    if out.exists():
        logger.info("Dubbed audio exists, skip.")
        return out

    data = json.loads(transcript_json.read_text())
    segments = data.get("segments", [])
    if not segments:
        logger.warning("No segments found, writing 1-s silence.")
        AudioSegment.silent(duration=1000, frame_rate=SAMPLE_RATE).export(out, format="wav")
        return out

    tts = _load()
    combined = AudioSegment.silent(duration=0, frame_rate=SAMPLE_RATE)
    for seg in segments:
        txt = seg["text"].strip()
        if not txt:
            continue
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        # **always** pass the default_speaker**
        tts.tts_to_file(
            text=txt,
            file_path=tmp.name,
            speaker=tts.default_speaker
        )
        seg_audio = AudioSegment.from_wav(tmp.name)
        combined += seg_audio

    combined.export(out, format="wav")
    logger.info("Wrote TTS audio → %s", out)
    return out
