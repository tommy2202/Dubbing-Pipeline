from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    """Centralized filesystem layout for pipeline v2."""

    work_dir: Path
    uploads_dir: Path
    outputs_dir: Path
    logs_dir: Path
    voices_dir: Path


def default_paths(work_dir: Path | None = None) -> ProjectPaths:
    base = work_dir or Path.cwd()
    return ProjectPaths(
        work_dir=base,
        uploads_dir=base / "uploads",
        outputs_dir=base / "Output",
        logs_dir=base / "logs",
        voices_dir=base / "voices",
    )


def output_root(work_dir: Path | None = None) -> Path:
    return default_paths(work_dir).outputs_dir


def output_dir_for(video_path: Path, work_dir: Path | None = None) -> Path:
    return output_root(work_dir) / video_path.stem


def voices_root(work_dir: Path | None = None) -> Path:
    return default_paths(work_dir).voices_dir


def voices_registry_path(work_dir: Path | None = None) -> Path:
    return voices_root(work_dir) / "registry.json"


def voices_embeddings_dir(work_dir: Path | None = None) -> Path:
    return voices_root(work_dir) / "embeddings"


def segments_dir(out_dir: Path) -> Path:
    return out_dir / "segments"
