from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LipSyncRequest:
    input_video: Path
    dubbed_audio_wav: Path
    output_video: Path
    work_dir: Path
    face_mode: str = "auto"  # auto|center|bbox
    device: str = "auto"  # auto|cpu|cuda
    bbox: tuple[int, int, int, int] | None = None  # x1,y1,x2,y2 (only when face_mode=bbox)
    # Feature J: scene-limited lip-sync
    scene_limited: bool = False
    ranges: list[tuple[float, float]] | None = (
        None  # explicit ranges (seconds) to apply lip-sync; gaps are pass-through
    )
    # face visibility gating for auto-range selection (best-effort; offline)
    sample_every_s: float = 0.5
    min_face_ratio: float = 0.60
    min_range_s: float = 2.0
    merge_gap_s: float = 0.6
    max_frames: int = 600
    timeout_s: int = 1200
    dry_run: bool = False


class LipSyncPlugin(ABC):
    """
    Optional post-processing plugin for lip-sync video generation.
    """

    name: str

    @abstractmethod
    def is_available(self) -> bool:
        """
        Returns True if the plugin can run on this machine with current configuration.
        """

    @abstractmethod
    def run(self, req: LipSyncRequest) -> Path:
        """
        Runs lip-sync and returns the output video path.
        Implementations must:
        - write all temp files under req.work_dir
        - not require internet
        - be safe to call when optional deps are missing (raise a clear error)
        """
