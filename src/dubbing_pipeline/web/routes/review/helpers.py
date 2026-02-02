from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException


def _parse_srt(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    blocks = [b for b in text.split("\n\n") if b.strip()]

    def parse_ts(ts: str) -> float:
        hh, mm, rest = ts.split(":")
        ss, ms = rest.split(",")
        return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0

    out: list[dict[str, Any]] = []
    for b in blocks:
        lines = [ln.rstrip("\n") for ln in b.splitlines() if ln.strip()]
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        try:
            start_s, end_s = (p.strip() for p in lines[1].split("-->", 1))
            start = float(parse_ts(start_s))
            end = float(parse_ts(end_s))
            txt = "\n".join(lines[2:]).strip() if len(lines) > 2 else ""
            out.append({"start": start, "end": end, "text": txt})
        except Exception:
            continue
    return out


def _review_state_path(base_dir: Path) -> Path:
    return (base_dir / "review" / "state.json").resolve()


def _review_audio_path(base_dir: Path, segment_id: int) -> Path | None:
    try:
        from dubbing_pipeline.review.state import load_state

        st = load_state(base_dir)
        segs = st.get("segments", [])
        if not isinstance(segs, list):
            return None
        for s in segs:
            if isinstance(s, dict) and int(s.get("segment_id") or 0) == int(segment_id):
                p = Path(str(s.get("audio_path_current") or "")).resolve()
                # Prevent arbitrary file reads: audio must live under this job's output folder.
                try:
                    p.relative_to(Path(base_dir).resolve())
                except Exception:
                    return None
                return p if p.exists() and p.is_file() else None
    except Exception:
        return None
    return None


def _fmt_ts_srt(seconds: float) -> str:
    s = max(0.0, float(seconds))
    hh = int(s // 3600)
    mm = int((s % 3600) // 60)
    ss = int(s % 60)
    ms = int(round((s - int(s)) * 1000.0))
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


def _write_srt_segments(path: Path, segments: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for i, s in enumerate(segments, 1):
            f.write(
                f"{i}\n{_fmt_ts_srt(float(s['start']))} --> {_fmt_ts_srt(float(s['end']))}\n{str(s.get('text') or '').strip()}\n\n"
            )


def _hash_text(text: str) -> str:
    t = " ".join(str(text or "").split())
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def _ensure_review_state(base_dir: Path, job_video_path: str | None) -> dict[str, Any]:
    rsp = _review_state_path(base_dir)
    if not rsp.exists():
        try:
            from dubbing_pipeline.review.ops import init_review

            init_review(base_dir, video_path=Path(job_video_path) if job_video_path else None)
        except Exception as ex:
            raise HTTPException(status_code=400, detail=f"review init failed: {ex}") from ex
    from dubbing_pipeline.review.state import load_state

    return load_state(base_dir)


def _segments_from_state(
    *,
    state: dict[str, Any],
    transcript_store: dict[str, Any],
    qa_by_segment: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    segs = state.get("segments", [])
    if not isinstance(segs, list):
        return []
    seg_over = transcript_store.get("segments", {}) if isinstance(transcript_store, dict) else {}
    if not isinstance(seg_over, dict):
        seg_over = {}
    out: list[dict[str, Any]] = []
    for s in segs:
        if not isinstance(s, dict):
            continue
        try:
            sid = int(s.get("segment_id") or 0)
        except Exception:
            continue
        if sid <= 0:
            continue
        ov = seg_over.get(str(sid), {})
        chosen = str(s.get("chosen_text") or s.get("translated_text") or "")
        if isinstance(ov, dict) and "tgt_text" in ov:
            chosen = str(ov.get("tgt_text") or "")
        qa = qa_by_segment.get(int(sid), {})
        qa_status = str(qa.get("status") or "").strip().lower()
        if not qa_status:
            qa_status = "approved" if bool(ov.get("approved")) else "pending"
        out.append(
            {
                "segment_id": int(sid),
                "start": float(s.get("start", 0.0)),
                "end": float(s.get("end", 0.0)),
                "speaker_id": str(s.get("speaker") or s.get("speaker_id") or "SPEAKER_01"),
                "source_text": str(s.get("source_text") or ""),
                "translated_text": str(s.get("translated_text") or ""),
                "chosen_text": chosen,
                "qa_status": qa_status,
                "qa_notes": qa.get("notes"),
                "qa_updated_at": qa.get("updated_at"),
                "edited_text": qa.get("edited_text"),
                "pronunciation_overrides": qa.get("pronunciation_overrides"),
                "glossary_used": qa.get("glossary_used"),
            }
        )
    return out


def _rewrite_helper_formal(text: str) -> str:
    """
    Deterministic "more formal" rewrite (best-effort, English-focused).
    """
    t = " ".join(str(text or "").split()).strip()
    if not t:
        return ""
    # Expand common contractions
    repls = [
        (r"(?i)\bcan't\b", "cannot"),
        (r"(?i)\bwon't\b", "will not"),
        (r"(?i)\bdon't\b", "do not"),
        (r"(?i)\bdoesn't\b", "does not"),
        (r"(?i)\bdidn't\b", "did not"),
        (r"(?i)\bisn't\b", "is not"),
        (r"(?i)\baren't\b", "are not"),
        (r"(?i)\bwasn't\b", "was not"),
        (r"(?i)\bweren't\b", "were not"),
        (r"(?i)\bit's\b", "it is"),
        (r"(?i)\bthat's\b", "that is"),
        (r"(?i)\bthere's\b", "there is"),
        (r"(?i)\bI'm\b", "I am"),
        (r"(?i)\bI've\b", "I have"),
        (r"(?i)\bI'll\b", "I will"),
        (r"(?i)\bwe're\b", "we are"),
        (r"(?i)\bthey're\b", "they are"),
        (r"(?i)\byou're\b", "you are"),
    ]
    for pat, rep in repls:
        t = re.sub(pat, rep, t)
    return t.strip()


def _rewrite_helper_reduce_slang(text: str) -> str:
    """
    Deterministic slang reduction (best-effort, English-focused).
    """
    t = " ".join(str(text or "").split()).strip()
    if not t:
        return ""
    slang = [
        (r"(?i)\bgonna\b", "going to"),
        (r"(?i)\bwanna\b", "want to"),
        (r"(?i)\bgotta\b", "have to"),
        (r"(?i)\bkinda\b", "somewhat"),
        (r"(?i)\bsorta\b", "somewhat"),
        (r"(?i)\bain't\b", "is not"),
        (r"(?i)\by'all\b", "you all"),
        (r"(?i)\bya\b", "you"),
    ]
    for pat, rep in slang:
        t = re.sub(pat, rep, t)
    return t.strip()
