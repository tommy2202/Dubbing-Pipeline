from __future__ import annotations

from pathlib import Path

from dubbing_pipeline.utils.time import format_srt_timestamp


def format_vtt_timestamp(seconds: float) -> str:
    """
    WebVTT timestamp: HH:MM:SS.mmm
    """
    s = max(0.0, float(seconds))
    hh = int(s // 3600)
    mm = int((s % 3600) // 60)
    ss = int(s % 60)
    ms = int(round((s - int(s)) * 1000.0))
    return f"{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}"


def write_srt(lines: list[dict], path: Path) -> None:
    """
    Write a minimal SRT from [{start,end,text}] or [{start,end,speaker_id,text}].
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for idx, line in enumerate(lines, 1):
            st = format_srt_timestamp(float(line["start"]))
            en = format_srt_timestamp(float(line["end"]))
            txt = str(line.get("text", "") or "").strip()
            f.write(f"{idx}\n{st} --> {en}\n{txt}\n\n")


def write_vtt(lines: list[dict], path: Path) -> None:
    """
    Write a minimal WebVTT from [{start,end,text}] or [{start,end,speaker_id,text}].

    Notes:
    - WebVTT allows cue identifiers; we omit them for simplicity.
    - We do not embed speaker metadata; callers can prefix speaker in text if desired.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for line in lines:
            st = format_vtt_timestamp(float(line["start"]))
            en = format_vtt_timestamp(float(line["end"]))
            txt = str(line.get("text", "") or "").strip()
            f.write(f"{st} --> {en}\n{txt}\n\n")
