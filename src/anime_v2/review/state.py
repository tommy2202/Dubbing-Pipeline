"""
Tier-2B review loop state + helpers.
"""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from typing import Any

from anime_v2.utils.io import atomic_write_text, read_json


def now_utc() -> str:
    return __import__("datetime").datetime.now(tz=__import__("datetime").UTC).isoformat()


def review_dir(job_dir: Path) -> Path:
    return Path(job_dir) / "review"


def review_state_path(job_dir: Path) -> Path:
    return review_dir(job_dir) / "state.json"


def review_audio_dir(job_dir: Path) -> Path:
    return review_dir(job_dir) / "audio"


def load_state(job_dir: Path) -> dict[str, Any]:
    return read_json(review_state_path(job_dir), default={"version": 1, "segments": [], "job": {}})


def save_state(job_dir: Path, state: dict[str, Any]) -> None:
    p = review_state_path(job_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _parse_srt(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    txt = path.read_text(encoding="utf-8", errors="replace")
    blocks = [b for b in txt.split("\n\n") if b.strip()]

    def parse_ts(ts: str) -> float:
        hh, mm, rest = ts.split(":")
        ss, ms = rest.split(",")
        return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0

    out: list[dict[str, Any]] = []
    for b in blocks:
        lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        try:
            start_s, end_s = (p.strip() for p in lines[1].split("-->", 1))
            start = float(parse_ts(start_s))
            end = float(parse_ts(end_s))
            text = " ".join(lines[2:]).strip()
            out.append({"start": start, "end": end, "text": text})
        except Exception:
            continue
    return out


def _best_clip_for_index(job_dir: Path, idx0: int) -> Path | None:
    """
    idx0 is 0-based segment index.
    """
    # Prefer manifest mapping
    man = Path(job_dir) / "tts_manifest.json"
    if man.exists():
        data = read_json(man, default={})
        if isinstance(data, dict):
            clips = data.get("clips")
            if isinstance(clips, list) and 0 <= idx0 < len(clips):
                try:
                    p = Path(str(clips[idx0]))
                    if p.exists():
                        return p
                except Exception:
                    pass
    # Fall back to matching file name in tts_clips
    clips_dir = Path(job_dir) / "tts_clips"
    if clips_dir.exists():
        pref = f"{idx0:04d}_"
        with suppress(Exception):
            for p in sorted(clips_dir.glob(f"{pref}*.wav")):
                if p.is_file():
                    return p
    return None


def init_state_from_job(
    *,
    job_dir: Path,
    video_path: Path | None,
    pipeline_params: dict[str, Any],
    voice_mapping_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build review/state.json from existing artifacts under Output/<job>/.
    """
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    review_dir(job_dir).mkdir(parents=True, exist_ok=True)
    review_audio_dir(job_dir).mkdir(parents=True, exist_ok=True)

    stem = job_dir.name
    translated_json = job_dir / "translated.json"
    translated_srt = job_dir / f"{stem}.translated.srt"
    src_srt = job_dir / f"{stem}.srt"

    segments: list[dict[str, Any]] = []

    if translated_json.exists():
        data = read_json(translated_json, default={})
        segs = data.get("segments", []) if isinstance(data, dict) else []
        if isinstance(segs, list):
            for i, s in enumerate(segs, 1):
                try:
                    start = float(s.get("start", 0.0))
                    end = float(s.get("end", 0.0))
                    speaker = str(s.get("speaker") or s.get("speaker_id") or "SPEAKER_01")
                    src_text = str(s.get("src_text") or "")
                    fitted = str(s.get("text") or "")
                    pre_fit = str(s.get("text_pre_fit") or "")
                    translated = pre_fit or fitted
                    chosen = fitted or translated
                    clip = _best_clip_for_index(job_dir, i - 1)
                    segments.append(
                        {
                            "segment_id": i,
                            "start": start,
                            "end": end,
                            "speaker": speaker,
                            "source_text": src_text,
                            "translated_text": translated,
                            "fitted_text": fitted if pre_fit else "",
                            "chosen_text": chosen,
                            "audio_path_current": str(clip) if clip else "",
                            "status": "pending",
                            "last_updated": now_utc(),
                            "notes": "",
                        }
                    )
                except Exception:
                    continue
    else:
        # SRT fallback
        src = _parse_srt(src_srt)
        tgt = _parse_srt(translated_srt if translated_srt.exists() else src_srt)
        n = max(len(src), len(tgt))
        for i in range(n):
            s0 = src[i] if i < len(src) else {"start": 0.0, "end": 0.0, "text": ""}
            t0 = tgt[i] if i < len(tgt) else {"start": s0["start"], "end": s0["end"], "text": ""}
            clip = _best_clip_for_index(job_dir, i)
            segments.append(
                {
                    "segment_id": i + 1,
                    "start": float(s0.get("start", 0.0)),
                    "end": float(s0.get("end", 0.0)),
                    "speaker": "SPEAKER_01",
                    "source_text": str(s0.get("text") or ""),
                    "translated_text": str(t0.get("text") or ""),
                    "fitted_text": "",
                    "chosen_text": str(t0.get("text") or ""),
                    "audio_path_current": str(clip) if clip else "",
                    "status": "pending",
                    "last_updated": now_utc(),
                    "notes": "",
                }
            )

    # If transcript_store.json exists (web editor), respect tgt_text overrides as chosen_text.
    with suppress(Exception):
        st = read_json(job_dir / "transcript_store.json", default={})
        seg_over = st.get("segments", {}) if isinstance(st, dict) else {}
        if isinstance(seg_over, dict):
            for seg in segments:
                sid = str(seg.get("segment_id"))
                ov = seg_over.get(sid)
                if isinstance(ov, dict) and "tgt_text" in ov:
                    seg["chosen_text"] = str(ov.get("tgt_text") or "")

    job_meta = {
        "created_at": now_utc(),
        "video_path": str(video_path) if video_path else "",
        "pipeline_params": dict(pipeline_params),
        "voice_mapping_snapshot": dict(voice_mapping_snapshot or {}),
    }

    state = {"version": 1, "job": job_meta, "segments": segments}
    return state


def find_segment(state: dict[str, Any], segment_id: int) -> dict[str, Any] | None:
    segs = state.get("segments", [])
    if not isinstance(segs, list):
        return None
    for s in segs:
        if isinstance(s, dict) and int(s.get("segment_id") or 0) == int(segment_id):
            return s
    return None


def short_preview(text: str, *, n: int = 60) -> str:
    t = " ".join(str(text or "").split())
    if len(t) <= n:
        return t
    return t[: max(0, n - 1)] + "…"


def render_audio_only(job_dir: Path, state: dict[str, Any], out_wav: Path) -> Path:
    """
    Build a full-length episode WAV using the segment audio paths in state.

    Uses the existing timeline compositor (overwrite semantics) from tts stage.
    """
    from anime_v2.stages.tts import render_aligned_track

    segs = state.get("segments", [])
    if not isinstance(segs, list) or not segs:
        raise ValueError("No segments in review state")

    lines: list[dict[str, Any]] = []
    clips: list[Path] = []
    for s in segs:
        if not isinstance(s, dict):
            continue
        try:
            start = float(s.get("start", 0.0))
            end = float(s.get("end", 0.0))
            p = Path(str(s.get("audio_path_current") or ""))
            if not p.exists():
                # missing audio => synthesize silence clip
                from anime_v2.stages.tts import _write_silence_wav

                sid = int(s.get("segment_id") or 0)
                p = review_audio_dir(job_dir) / f"_silence_{sid}.wav"
                _write_silence_wav(p, duration_s=max(0.0, end - start))
            lines.append({"start": start, "end": end, "text": str(s.get("chosen_text") or "")})
            clips.append(p)
        except Exception:
            continue

    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    render_aligned_track(lines, clips, out_wav)
    return out_wav


def bump_status(seg: dict[str, Any], status: str) -> None:
    seg["status"] = str(status)
    seg["last_updated"] = now_utc()

