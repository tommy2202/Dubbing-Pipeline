from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.log import logger


@dataclass(frozen=True, slots=True)
class JobContext:
    """
    Minimal job-scoped context object.

    This is intentionally lightweight and import-safe:
    - no model loading
    - no ffmpeg calls
    """

    job_id: str
    job_dir: Path
    settings_snapshot: dict[str, Any]

    @property
    def manifests_dir(self) -> Path:
        return self.job_dir / "manifests"

    @property
    def logs_dir(self) -> Path:
        return self.job_dir / "logs"

    def bind_logger(self, **fields: Any):
        return logger.bind(job_id=self.job_id, **fields)


def make_job_context(*, job_id: str, job_dir: Path) -> JobContext:
    s = get_settings()
    # Store only public settings here to avoid accidental secret persistence.
    snap = s.public.model_dump()
    snap_s: dict[str, Any] = {}
    for k, v in snap.items():
        try:
            snap_s[k] = str(v) if hasattr(v, "__fspath__") else v
        except Exception:
            snap_s[k] = str(v)
    return JobContext(job_id=str(job_id), job_dir=Path(job_dir), settings_snapshot=snap_s)
