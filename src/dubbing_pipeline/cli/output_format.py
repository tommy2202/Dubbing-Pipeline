from __future__ import annotations

from pathlib import Path

from dubbing_pipeline.utils.subtitles import write_vtt
from dubbing_pipeline.utils.time import format_srt_timestamp


def _write_srt_from_lines(lines: list[dict], srt_path: Path) -> None:
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    with srt_path.open("w", encoding="utf-8") as f:
        for idx, line in enumerate(lines, 1):
            st = format_srt_timestamp(float(line["start"]))
            en = format_srt_timestamp(float(line["end"]))
            txt = str(line.get("text", "") or "").strip()
            f.write(f"{idx}\n{st} --> {en}\n{txt}\n\n")


def _write_vtt_from_lines(lines: list[dict], vtt_path: Path) -> None:
    # Reuse SRT parsing format (start/end/text)
    write_vtt(
        [
            {
                "start": float(line["start"]),
                "end": float(line["end"]),
                "text": str(line.get("text") or ""),
            }
            for line in lines
        ],
        vtt_path,
    )
