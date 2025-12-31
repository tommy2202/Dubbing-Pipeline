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


def default_paths(work_dir: Path | None = None) -> ProjectPaths:
    base = work_dir or Path.cwd()
    return ProjectPaths(
        work_dir=base,
        uploads_dir=base / "uploads",
        outputs_dir=base / "outputs",
        logs_dir=base / "logs",
    )

