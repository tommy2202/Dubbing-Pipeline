from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from anime_v2.config import get_settings


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    """Centralized filesystem layout for pipeline v2."""

    work_dir: Path
    uploads_dir: Path
    outputs_dir: Path
    logs_dir: Path
    voices_dir: Path


def default_paths(work_dir: Path | None = None) -> ProjectPaths:
    if work_dir is not None:
        base = Path(work_dir)
        return ProjectPaths(
            work_dir=base,
            uploads_dir=base / "uploads",
            outputs_dir=base / "Output",
            logs_dir=base / "logs",
            voices_dir=base / "voices",
        )

    # Default to configured, repo-wide settings (avoids hardcoded cwd assumptions).
    s = get_settings()
    root = Path(s.app_root).resolve()
    out_root = Path(s.output_dir).resolve()
    logs_root = Path(s.log_dir).resolve()
    voices_root = (root / "voices").resolve()
    uploads_root = None
    if getattr(s, "input_uploads_dir", None):
        with suppress(Exception):
            uploads_root = Path(str(s.input_uploads_dir)).resolve()
    if uploads_root is None and getattr(s, "input_dir", None):
        with suppress(Exception):
            uploads_root = (Path(str(s.input_dir)).resolve() / "uploads").resolve()
    if uploads_root is None:
        uploads_root = (root / "Input" / "uploads").resolve()
    return ProjectPaths(
        work_dir=root,
        uploads_dir=uploads_root,
        outputs_dir=out_root,
        logs_dir=logs_root,
        voices_dir=voices_root,
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
