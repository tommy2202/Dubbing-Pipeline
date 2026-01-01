from __future__ import annotations

import os
import re
import shutil
import sys
from contextlib import suppress
from pathlib import Path

from anime_v2.config import get_settings
from anime_v2.review.state import (
    bump_status,
    find_segment,
    init_state_from_job,
    load_state,
    now_utc,
    review_audio_dir,
    review_state_path,
    save_state,
)
from anime_v2.utils.io import atomic_copy, atomic_write_text
from anime_v2.utils.log import logger


def resolve_job_dir(job: str) -> Path:
    p = Path(job)
    if p.exists() and p.is_dir():
        return p.resolve()
    return (Path(get_settings().output_dir).resolve() / str(job)).resolve()


def ensure_review_state(*, job_dir: Path, video_path: Path | None = None) -> Path:
    job_dir = Path(job_dir)
    p = review_state_path(job_dir)
    if p.exists():
        return p
    raise FileNotFoundError(f"Missing review state: {p} (run `anime-v2 review init ...`)")


def init_review(job_dir: Path, *, video_path: Path | None) -> Path:
    job_dir = Path(job_dir)
    stem = job_dir.name
    # require at least transcript to exist
    has_any = (job_dir / f"{stem}.srt").exists() or (job_dir / "translated.json").exists()
    if not has_any:
        raise FileNotFoundError(
            f"No transcript artifacts found under {job_dir}. Run the pipeline first."
        )

    s = get_settings()
    state = init_state_from_job(
        job_dir=job_dir,
        video_path=video_path,
        pipeline_params={
            "timing_fit": bool(s.timing_fit),
            "pacing": bool(s.pacing),
            "pacing_min_ratio": float(s.pacing_min_ratio),
            "pacing_max_ratio": float(s.pacing_max_ratio),
            "timing_tolerance": float(s.timing_tolerance),
            "voice_memory": bool(s.voice_memory),
            "voice_match_threshold": float(s.voice_match_threshold),
            "voice_auto_enroll": bool(s.voice_auto_enroll),
        },
        voice_mapping_snapshot={},
    )
    save_state(job_dir, state)
    return review_state_path(job_dir)


def edit_segment(job_dir: Path, segment_id: int, *, text: str) -> None:
    st = load_state(job_dir)
    seg = find_segment(st, int(segment_id))
    if seg is None:
        raise KeyError(f"segment_id {segment_id} not found")
    if str(seg.get("status")) == "locked":
        raise RuntimeError("Segment is locked; unlock before editing.")
    seg["chosen_text"] = str(text)
    bump_status(seg, "regenerated")
    save_state(job_dir, st)


def _next_audio_version(audio_dir: Path, segment_id: int) -> int:
    pat = re.compile(rf"^{int(segment_id)}_v(\d+)\.wav$")
    best = 0
    if audio_dir.exists():
        for p in audio_dir.glob(f"{int(segment_id)}_v*.wav"):
            m = pat.match(p.name)
            if m:
                with suppress(Exception):
                    best = max(best, int(m.group(1)))
    return best + 1


def regen_segment(job_dir: Path, segment_id: int) -> Path:
    job_dir = Path(job_dir)
    st = load_state(job_dir)
    seg = find_segment(st, int(segment_id))
    if seg is None:
        raise KeyError(f"segment_id {segment_id} not found")
    if str(seg.get("status")) == "locked":
        raise RuntimeError("Segment is locked; unlock before regenerating.")

    start = float(seg.get("start", 0.0))
    end = float(seg.get("end", 0.0))
    dur = max(0.05, end - start)
    speaker = str(seg.get("speaker") or "SPEAKER_01")
    text = str(seg.get("chosen_text") or "")

    audio_dir = review_audio_dir(job_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)
    v = _next_audio_version(audio_dir, int(segment_id))
    out = audio_dir / f"{int(segment_id)}_v{v}.wav"

    # Use the existing TTS stage as a single-segment synthesizer (with fallbacks).
    tmp_dir = job_dir / "review" / "tmp" / f"{int(segment_id)}"
    if tmp_dir.exists():
        with suppress(Exception):
            shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Minimal translated.json for single segment (start at 0 so output is a clip).
    one_json = tmp_dir / "translated.json"
    atomic_write_text(
        one_json,
        __import__("json").dumps(
            {
                "src_lang": "",
                "tgt_lang": "",
                "segments": [
                    {
                        "start": 0.0,
                        "end": float(dur),
                        "speaker": speaker,
                        "src_text": str(seg.get("source_text") or ""),
                        "text": text,
                        "text_pre_fit": str(seg.get("translated_text") or ""),
                    }
                ],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    diar_json = job_dir / "diarization.json"
    diar = diar_json if diar_json.exists() else None

    from anime_v2.stages import tts as tts_stage

    # Use snapshot if present; fall back to current settings.
    params = dict((st.get("job") or {}).get("pipeline_params") or {})
    pacing = bool(params.get("pacing", get_settings().pacing))
    try:
        tts_wav = tmp_dir / "tts.wav"
        tts_stage.run(
            out_dir=tmp_dir,
            translated_json=one_json,
            diarization_json=diar,
            wav_out=tts_wav,
            pacing=pacing,
            pacing_min_ratio=float(
                params.get("pacing_min_ratio", getattr(get_settings(), "pacing_min_ratio", 0.88))
            ),
            pacing_max_ratio=float(
                params.get("pacing_max_ratio", getattr(get_settings(), "pacing_max_ratio", 1.18))
            ),
            timing_tolerance=float(
                params.get(
                    "timing_tolerance", getattr(get_settings(), "timing_tolerance", 0.10)
                )
            ),
            voice_memory=bool(params.get("voice_memory", getattr(get_settings(), "voice_memory", False))),
            voice_match_threshold=float(
                params.get(
                    "voice_match_threshold", getattr(get_settings(), "voice_match_threshold", 0.75)
                )
            ),
            voice_auto_enroll=bool(
                params.get("voice_auto_enroll", getattr(get_settings(), "voice_auto_enroll", True))
            ),
            voice_memory_dir=Path(get_settings().voice_memory_dir).resolve()
            if getattr(get_settings(), "voice_memory_dir", None)
            else None,
        )
        # Grab the first clip as the segment audio
        clip = None
        with suppress(Exception):
            for p in sorted((tmp_dir / "tts_clips").glob("*.wav")):
                clip = p
                break
        if clip is None or not Path(clip).exists():
            raise RuntimeError("TTS did not produce clip")
        atomic_copy(Path(clip), out)
    except Exception as ex:
        # hard fallback: silence clip (never crash)
        logger.warning("review_regen_failed", segment_id=int(segment_id), error=str(ex))
        from anime_v2.stages.tts import _write_silence_wav

        _write_silence_wav(out, duration_s=float(dur))

    seg["audio_path_current"] = str(out)
    bump_status(seg, "regenerated")
    save_state(job_dir, st)
    return out


def lock_segment(job_dir: Path, segment_id: int) -> None:
    st = load_state(job_dir)
    seg = find_segment(st, int(segment_id))
    if seg is None:
        raise KeyError(f"segment_id {segment_id} not found")
    p = Path(str(seg.get("audio_path_current") or ""))
    if not p.exists():
        raise RuntimeError("Cannot lock: audio_path_current is missing. Run review regen first.")
    bump_status(seg, "locked")
    save_state(job_dir, st)


def unlock_segment(job_dir: Path, segment_id: int) -> None:
    st = load_state(job_dir)
    seg = find_segment(st, int(segment_id))
    if seg is None:
        raise KeyError(f"segment_id {segment_id} not found")
    bump_status(seg, "regenerated")
    save_state(job_dir, st)


def play_segment(job_dir: Path, segment_id: int) -> Path:
    st = load_state(job_dir)
    seg = find_segment(st, int(segment_id))
    if seg is None:
        raise KeyError(f"segment_id {segment_id} not found")
    p = Path(str(seg.get("audio_path_current") or ""))
    if not p.exists():
        raise FileNotFoundError("audio_path_current missing (regen first)")

    # Best-effort open with OS default; always return path.
    try:
        if os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            __import__("subprocess").run(["open", str(p)], check=False)
        else:
            __import__("subprocess").run(["xdg-open", str(p)], check=False)
    except Exception:
        pass
    return p


def render(job_dir: Path) -> dict[str, Path]:
    """
    Build full episode audio from segment audio files and mux to a review MKV when possible.
    """
    job_dir = Path(job_dir)
    st = load_state(job_dir)
    out_dir = job_dir / "review"
    out_dir.mkdir(parents=True, exist_ok=True)

    out_wav = out_dir / "review_render.wav"
    from anime_v2.review.state import render_audio_only

    render_audio_only(job_dir, st, out_wav)

    outs: dict[str, Path] = {"wav": out_wav}

    # Mux if we have a source video
    vpath = str((st.get("job") or {}).get("video_path") or "")
    if vpath:
        src_video = Path(vpath)
        if src_video.exists():
            try:
                from anime_v2.stages.mkv_export import mux

                mkv = out_dir / "dub.review.mkv"
                mux(src_video=src_video, dub_wav=out_wav, srt_path=None, out_mkv=mkv)
                outs["mkv"] = mkv
            except Exception as ex:
                logger.warning("review_mux_failed", error=str(ex))

    # update state snapshot
    (st.setdefault("job", {}) if isinstance(st.get("job"), dict) else st).__setitem__(
        "last_rendered_at", now_utc()
    )
    save_state(job_dir, st)
    return outs


def lock_from_tts_manifest(
    *,
    job_dir: Path,
    tts_manifest: Path,
    video_path: Path | None,
    lock_nonempty_only: bool = True,
) -> int:
    """
    Canonical bridge from the legacy "resynth approved" flow into Tier-2B review state.

    Reads a tts_manifest.json produced by `anime_v2.stages.tts.run` and:
    - copies each clip into Output/<job>/review/audio/<segment_id>_vN.wav
    - updates Output/<job>/review/state.json
    - locks segments with non-empty text (by default)
    """
    from anime_v2.utils.io import read_json

    job_dir = Path(job_dir)
    tts_manifest = Path(tts_manifest)
    if not tts_manifest.exists():
        raise FileNotFoundError(f"Missing tts_manifest: {tts_manifest}")
    data = read_json(tts_manifest, default={})
    if not isinstance(data, dict):
        raise ValueError("Invalid tts_manifest JSON")
    clips = data.get("clips", [])
    lines = data.get("lines", [])
    if not isinstance(clips, list) or not isinstance(lines, list) or len(clips) != len(lines):
        raise ValueError("Invalid tts_manifest: expected equal-length clips + lines")

    # Ensure state exists.
    sp = review_state_path(job_dir)
    if not sp.exists():
        st = init_state_from_job(
            job_dir=job_dir, video_path=video_path, pipeline_params={}, voice_mapping_snapshot={}
        )
        save_state(job_dir, st)

    st = load_state(job_dir)
    locked = 0

    audio_dir = review_audio_dir(job_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)

    for i, (clip_s, line) in enumerate(zip(clips, lines, strict=False), 1):
        if not isinstance(line, dict):
            continue
        text = str(line.get("text") or "").strip()
        if lock_nonempty_only and not text:
            continue

        clip = Path(str(clip_s))
        if not clip.exists():
            continue

        seg = find_segment(st, int(i))
        if seg is None:
            continue

        v = _next_audio_version(audio_dir, int(i))
        dest = audio_dir / f"{int(i)}_v{v}.wav"
        atomic_copy(clip, dest)
        seg["chosen_text"] = text
        seg["audio_path_current"] = str(dest)
        bump_status(seg, "locked")
        locked += 1

    save_state(job_dir, st)
    return locked

