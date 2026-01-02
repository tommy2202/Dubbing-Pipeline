from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from anime_v2.streaming.runner import run_streaming


@dataclass(frozen=True, slots=True)
class RealtimeResult:
    out_dir: Path
    manifest_path: Path
    stitched_wav: Path | None
    stitched_srt: Path | None
    stitched_vtt: Path | None


def realtime_dub(
    *,
    video: Path,
    out_dir: Path,
    device: str,
    asr_model: str,
    src_lang: str,
    tgt_lang: str,
    mt_engine: str,
    mt_lowconf_thresh: float,
    glossary: str | None,
    style: str | None,
    chunk_seconds: float,
    chunk_overlap: float,
    stitch: bool,
    subs_choice: str,
    subs_format: str,
    align_mode: str,
    emotion_mode: str,
    expressive: str,
    expressive_strength: float,
    expressive_debug: bool,
    speech_rate: float,
    pitch: float,
    energy: float,
) -> RealtimeResult:
    """
    Backwards compatible wrapper around Tier-3C streaming runner.

    Historically this produced audio-only stitched artifacts under Output/<job>/realtime/.
    Tier-3C produces chunk MP4s under Output/<job>/stream/ and optionally a stitched final MP4.
    """
    video = Path(video)
    out_dir = Path(out_dir)
    rt_dir = out_dir / "realtime"
    rt_dir.mkdir(parents=True, exist_ok=True)

    res = run_streaming(
        video=video,
        out_dir=out_dir,
        device=device,
        asr_model=asr_model,
        src_lang=src_lang,
        tgt_lang=tgt_lang,
        mt_engine=mt_engine,
        mt_lowconf_thresh=float(mt_lowconf_thresh),
        glossary=glossary,
        style=style,
        stream=True,
        chunk_seconds=float(chunk_seconds),
        overlap_seconds=float(chunk_overlap),
        stream_output=("final" if stitch else "segments"),
        stream_concurrency=1,
        timing_fit=False,
        pacing=True,
        pacing_min_ratio=0.88,
        pacing_max_ratio=1.18,
        timing_tolerance=0.10,
        align_mode=str(align_mode),
        emotion_mode=str(emotion_mode),
        expressive=str(expressive),
        expressive_strength=float(expressive_strength),
        expressive_debug=bool(expressive_debug),
        speech_rate=float(speech_rate),
        pitch=float(pitch),
        energy=float(energy),
    )

    # Provide legacy-shaped return (best-effort).
    man = Path(str(res.get("manifest")))
    stitched = Path(str(res.get("final"))) if res.get("final") else None
    if stitched is not None and stitched.exists():
        with suppress(Exception):
            (rt_dir / "realtime.stitched.mp4").write_bytes(stitched.read_bytes())
    return RealtimeResult(out_dir=rt_dir, manifest_path=man, stitched_wav=None, stitched_srt=None, stitched_vtt=None)
