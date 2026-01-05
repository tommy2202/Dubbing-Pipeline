from __future__ import annotations

import math
import wave
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from anime_v2.utils.log import logger


@dataclass(frozen=True, slots=True)
class VADConfig:
    sample_rate: int = 16000
    frame_ms: int = 30  # 10/20/30 supported by webrtcvad
    aggressiveness: int = 2  # 0..3
    energy_gate: float = 0.0005  # RMS gate (heuristic)
    min_speech_ms: int = 300
    min_silence_ms: int = 250


def _rms_int16(frame: bytes) -> float:
    # frame is little-endian int16 PCM
    if not frame:
        return 0.0
    n = len(frame) // 2
    if n <= 0:
        return 0.0
    s = 0.0
    for i in range(0, len(frame), 2):
        v = int.from_bytes(frame[i : i + 2], "little", signed=True)
        s += float(v * v)
    return math.sqrt(s / n) / 32768.0


def detect_speech_segments(
    wav_path: str | Path, cfg: VADConfig = VADConfig()
) -> list[tuple[float, float]]:
    """
    Return speech segments [(start_s, end_s)] using:
      - webrtcvad if available, gated by energy
      - fallback to pure energy gate if not available
    Assumes 16kHz mono PCM WAV (pipeline extracts this).
    """
    path = Path(wav_path)
    if not path.exists():
        return []

    try:
        import webrtcvad  # type: ignore

        vad = webrtcvad.Vad(int(cfg.aggressiveness))
        use_webrtc = True
    except Exception:
        vad = None
        use_webrtc = False

    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        if sr != cfg.sample_rate or ch != 1 or sw != 2:
            logger.warning("VAD expects 16kHz mono int16; got sr=%s ch=%s sw=%s", sr, ch, sw)

        frames: list[tuple[float, float, bool]] = []
        t = 0.0
        while True:
            buf = wf.readframes(int(sr * (cfg.frame_ms / 1000.0)))
            if not buf:
                break
            rms = _rms_int16(buf)
            speech = rms >= cfg.energy_gate
            if use_webrtc and speech:
                with suppress(Exception):
                    speech = bool(vad.is_speech(buf, sr))
            frames.append((t, t + cfg.frame_ms / 1000.0, speech))
            t += cfg.frame_ms / 1000.0

    # Merge frames into segments with min silence
    segs: list[tuple[float, float]] = []
    cur_start = None
    last_speech_end = None
    for start, end, speech in frames:
        if speech:
            if cur_start is None:
                cur_start = start
            last_speech_end = end
        else:
            if cur_start is not None and last_speech_end is not None:
                silence_ms = (end - last_speech_end) * 1000.0
                if silence_ms >= cfg.min_silence_ms:
                    segs.append((cur_start, last_speech_end))
                    cur_start = None
                    last_speech_end = None

    if cur_start is not None and last_speech_end is not None:
        segs.append((cur_start, last_speech_end))

    # Filter short segments and merge small gaps
    out: list[tuple[float, float]] = []
    for s, e in segs:
        if (e - s) * 1000.0 < cfg.min_speech_ms:
            continue
        if out and s - out[-1][1] <= cfg.min_silence_ms / 1000.0:
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out
