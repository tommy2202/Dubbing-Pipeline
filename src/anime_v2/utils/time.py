from __future__ import annotations


def format_srt_timestamp(seconds: float) -> str:
    """
    Convert seconds to SRT timestamp: HH:MM:SS,mmm
    """
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

