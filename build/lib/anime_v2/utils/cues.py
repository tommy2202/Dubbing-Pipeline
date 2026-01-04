from __future__ import annotations

from pathlib import Path


def parse_srt_to_cues(srt_path: Path) -> list[dict]:
    """
    Parse SRT into cues: [{start, end, text}] (seconds).
    """
    if not srt_path.exists():
        return []
    text = srt_path.read_text(encoding="utf-8", errors="replace")
    blocks = [b for b in text.split("\n\n") if b.strip()]
    cues: list[dict] = []

    def parse_ts(ts: str) -> float:
        hh, mm, rest = ts.split(":")
        ss, ms = rest.split(",")
        return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0

    for b in blocks:
        lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        start_s, end_s = (p.strip() for p in lines[1].split("-->", 1))
        try:
            start = parse_ts(start_s)
            end = parse_ts(end_s)
        except Exception:
            continue
        cue_text = " ".join(lines[2:]).strip() if len(lines) > 2 else ""
        cues.append({"start": start, "end": end, "text": cue_text})
    return cues


def assign_speakers(cues: list[dict], diar_segments: list[dict] | None) -> list[dict]:
    """
    Assign a speaker_id to each cue by midpoint overlap with diarization segments.
    """
    diar_segments = diar_segments or []
    out: list[dict] = []
    for c in cues:
        start = float(c["start"])
        end = float(c["end"])
        mid = (start + end) / 2.0
        speaker_id = "Speaker1"
        for seg in diar_segments:
            try:
                if float(seg["start"]) <= mid <= float(seg["end"]):
                    speaker_id = str(seg.get("speaker_id") or speaker_id)
                    break
            except Exception:
                continue
        out.append(
            {
                "start": start,
                "end": end,
                "speaker_id": speaker_id,
                "text": str(c.get("text", "") or ""),
            }
        )
    return out

