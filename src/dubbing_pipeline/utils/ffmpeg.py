from __future__ import annotations

# Canonical ffmpeg helpers for the pipeline.
#
# This module is intentionally a thin wrapper over `dubbing_pipeline.utils.ffmpeg_safe`:
# - list-argv only (no shell)
# - friendly errors with stderr tail when available
# - optional timeouts/retries
from pathlib import Path

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.ffmpeg_safe import (
    FFmpegError,
    extract_audio_mono_16k,
    ffprobe_duration_seconds,
    run_ffmpeg,
)

__all__ = [
    "FFmpegError",
    "run_ffmpeg",
    "ffprobe_duration_seconds",
    "extract_audio_mono_16k",
    "ensure_wav_44k_stereo",
]


def ensure_wav_44k_stereo(src_wav: Path, dst_wav: Path, *, timeout_s: int = 120) -> Path:
    """
    Convert WAV (or any audio container) to a demucs-friendly WAV:
    - stereo
    - 44.1kHz
    """
    argv = [
        str(get_settings().ffmpeg_bin),
        "-y",
        "-i",
        str(src_wav),
        "-ac",
        "2",
        "-ar",
        "44100",
        str(dst_wav),
    ]
    run_ffmpeg(argv, timeout_s=timeout_s, retries=1, capture=True)
    return dst_wav
