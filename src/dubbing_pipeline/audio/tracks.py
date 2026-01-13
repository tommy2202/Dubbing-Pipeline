from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dubbing_pipeline.utils.io import atomic_copy
from dubbing_pipeline.utils.log import logger


@dataclass(frozen=True, slots=True)
class TrackArtifacts:
    original_full_wav: Path
    dubbed_full_wav: Path
    background_only_wav: Path
    dialogue_only_wav: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_full_wav": str(self.original_full_wav),
            "dubbed_full_wav": str(self.dubbed_full_wav),
            "background_only_wav": str(self.background_only_wav),
            "dialogue_only_wav": str(self.dialogue_only_wav),
        }


def build_multitrack_artifacts(
    *,
    job_dir: Path,
    original_wav: Path,
    dubbed_wav: Path,
    dialogue_wav: Path,
    background_wav: Path | None,
) -> TrackArtifacts:
    """
    Create deterministic per-job audio artifacts for multitrack muxing.

    Output layout:
      Output/<job>/audio/tracks/
        - original_full.wav
        - dubbed_full.wav
        - dialogue_only.wav
        - background_only.wav (when provided)
    """
    job_dir = Path(job_dir)
    tracks_dir = (job_dir / "audio" / "tracks").resolve()
    tracks_dir.mkdir(parents=True, exist_ok=True)

    out_orig = tracks_dir / "original_full.wav"
    out_dub = tracks_dir / "dubbed_full.wav"
    out_dlg = tracks_dir / "dialogue_only.wav"
    out_bg = tracks_dir / "background_only.wav"

    atomic_copy(Path(original_wav), out_orig)
    atomic_copy(Path(dubbed_wav), out_dub)
    atomic_copy(Path(dialogue_wav), out_dlg)
    bg_source = Path(background_wav) if background_wav is not None else Path(original_wav)
    if not bg_source.exists():
        bg_source = Path(original_wav)
    atomic_copy(bg_source, out_bg)
    logger.info(
        "multitrack_artifacts_ready",
        job_dir=str(job_dir),
        tracks_dir=str(tracks_dir),
        background_source=str(bg_source),
    )
    return TrackArtifacts(
        original_full_wav=out_orig,
        dubbed_full_wav=out_dub,
        background_only_wav=out_bg,
        dialogue_only_wav=out_dlg,
    )
