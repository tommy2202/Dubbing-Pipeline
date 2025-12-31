from __future__ import annotations

import shutil
from pathlib import Path

from anime_v2.utils.log import logger


def run(transcript_srt: Path, wav_out: Path, ckpt_dir: Path, **_) -> Path:
    """
    Text-to-speech stage.

    Stub: creates a placeholder WAV so downstream muxing can proceed.
    """
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    wav_out.parent.mkdir(parents=True, exist_ok=True)
    logger.info("[v2] tts.run(transcript=%s) -> %s", transcript_srt, wav_out)

    # Minimal placeholder: copy an existing wav if present, else touch file.
    src = ckpt_dir / "audio.wav"
    if src.exists():
        shutil.copy2(src, wav_out)
    else:
        wav_out.write_bytes(b"")
    return wav_out

