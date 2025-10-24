import pathlib
import json
import tempfile
import subprocess
from typing import Optional, List
from anime_v1.utils import logger, checkpoints
from pydub import AudioSegment

SAMPLE_RATE = 22050
_tts_model = None


def _load_coqui(model_name: str = "tts_models/en/vctk/vits"):
    global _tts_model
    if _tts_model is not None:
        return _tts_model
    try:
        # Lazy import to allow environments without Coqui
        from TTS.api import TTS  # type: ignore
    except Exception as ex:  # pragma: no cover
        logger.warning("Coqui TTS not available (%s)", ex)
        return None
    try:
        logger.info("Loading Coqui TTS %s …", model_name)
        _tts_model = TTS(model_name=model_name, progress_bar=False, gpu=False)
        # Select a default speaker if available
        if getattr(_tts_model, "speakers", None):
            _tts_model.default_speaker = _tts_model.speakers[0]
        return _tts_model
    except Exception as ex:  # pragma: no cover
        logger.warning("Failed to load Coqui TTS (%s)", ex)
        return None


def _speak_with_coqui(tts, text: str, outfile: pathlib.Path) -> bool:
    try:
        kwargs = {"text": text, "file_path": str(outfile)}
        # Speaker (if model supports multi-speaker)
        if getattr(tts, "default_speaker", None):
            kwargs["speaker"] = tts.default_speaker
        tts.tts_to_file(**kwargs)
        return True
    except Exception as ex:  # pragma: no cover
        logger.warning("Coqui synthesis failed (%s)", ex)
        return False


def _speak_with_espeak(text: str, outfile: pathlib.Path, lang: str = "en", speed_wpm: int = 175) -> bool:
    try:
        # espeak-ng writes WAV via -w
        cmd = [
            "espeak-ng",
            f"-v{lang}",
            f"-s{speed_wpm}",
            "-w", str(outfile),
            text,
        ]
        subprocess.run(cmd, check=True)
        return True
    except Exception as ex:  # pragma: no cover
        logger.warning("espeak-ng fallback failed (%s)", ex)
        return False


def _time_stretch_with_ffmpeg(in_wav: pathlib.Path, out_wav: pathlib.Path, tempo: float) -> bool:
    """Time-stretch using ffmpeg atempo (chain if out of range)."""
    try:
        # Build a chain of atempo filters within [0.5, 2.0]
        factors: List[float] = []
        remaining = tempo
        while remaining > 2.0:
            factors.append(2.0)
            remaining /= 2.0
        while 0 < remaining < 0.5:
            factors.append(0.5)
            remaining /= 0.5
        if remaining > 0:
            factors.append(remaining)
        filt = ",".join(f"atempo={f:.5f}" for f in factors)
        cmd = [
            "ffmpeg", "-y", "-i", str(in_wav),
            "-filter:a", filt,
            str(out_wav),
        ]
        subprocess.run(cmd, check=True)
        return True
    except Exception as ex:  # pragma: no cover
        logger.warning("ffmpeg atempo failed (%s)", ex)
        return False


def _align_to_duration(seg_wav: pathlib.Path, target_ms: int) -> AudioSegment:
    audio = AudioSegment.from_wav(seg_wav)
    if target_ms <= 0:
        return audio
    current_ms = max(1, int(len(audio)))
    if current_ms == target_ms:
        return audio
    tempo = current_ms / max(1, target_ms)
    tmp_out = pathlib.Path(seg_wav.parent) / (seg_wav.stem + ".tempo.wav")
    if 0.25 <= tempo <= 4.0 and _time_stretch_with_ffmpeg(seg_wav, tmp_out, tempo):
        try:
            return AudioSegment.from_wav(tmp_out)
        finally:
            tmp_out.unlink(missing_ok=True)
    # Fallback: pad or trim without time-stretch
    if current_ms < target_ms:
        return audio + AudioSegment.silent(duration=(target_ms - current_ms), frame_rate=audio.frame_rate)
    return audio[:target_ms]


def run(
    transcript_json: pathlib.Path,
    ckpt_dir: pathlib.Path,
    *,
    tgt_lang: str = "en",
    preference: str = "default",
    source_audio: Optional[pathlib.Path] = None,
    **_,
):
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

    tts = _load_coqui()
    combined = AudioSegment.silent(duration=0, frame_rate=SAMPLE_RATE)
    cursor_ms = 0
    produced_any = False

    for seg in segments:
        txt = (seg.get("text") or "").strip()
        if not txt:
            continue
        start_ms = int(float(seg.get("start", 0.0)) * 1000)
        end_ms = int(float(seg.get("end", 0.0)) * 1000)
        target_ms = max(0, end_ms - start_ms)

        tmp = pathlib.Path(tempfile.mkstemp(suffix=".wav")[1])

        ok = False
        if tts is not None:
            ok = _speak_with_coqui(tts, txt, tmp)
        if not ok:
            # Fallback to espeak-ng
            logger.info("Using espeak-ng fallback for segment")
            ok = _speak_with_espeak(txt, tmp, lang=(tgt_lang or "en"))

        if not ok:
            logger.warning("Skipping segment due to TTS failures")
            tmp.unlink(missing_ok=True)
            continue

        seg_audio = _align_to_duration(tmp, target_ms)
        tmp.unlink(missing_ok=True)

        # Insert silence up to segment start
        if start_ms > cursor_ms:
            combined += AudioSegment.silent(duration=(start_ms - cursor_ms), frame_rate=SAMPLE_RATE)
            cursor_ms = start_ms

        # Append aligned segment and advance cursor
        # Resample to desired SAMPLE_RATE if needed
        if seg_audio.frame_rate != SAMPLE_RATE:
            seg_audio = seg_audio.set_frame_rate(SAMPLE_RATE)
        combined += seg_audio
        cursor_ms += len(seg_audio)
        produced_any = True

    if not produced_any:
        logger.warning("No audio produced; writing 1-s silence.")
        AudioSegment.silent(duration=1000, frame_rate=SAMPLE_RATE).export(out, format="wav")
        return out

    combined.export(out, format="wav")
    logger.info("Wrote TTS audio → %s", out)
    return out
