from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anime_v2.utils.io import atomic_write_text, ensure_dir
from anime_v2.utils.log import redact_event


@dataclass(frozen=True, slots=True)
class JobLogPaths:
    root: Path
    pipeline_jsonl: Path
    pipeline_txt: Path
    stages_dir: Path
    ffmpeg_dir: Path
    summary_json: Path


def job_log_paths(job_dir: Path) -> JobLogPaths:
    root = Path(job_dir) / "logs"
    return JobLogPaths(
        root=root,
        pipeline_jsonl=root / "pipeline.log",
        pipeline_txt=root / "pipeline.txt",
        stages_dir=root / "stages",
        ffmpeg_dir=root / "ffmpeg",
        summary_json=root / "summary.json",
    )


class JobLogger:
    """
    Per-job logging helper.

    This does not replace structlog; it adds deterministic per-job artifacts:
      - Output/<job>/logs/pipeline.log (JSONL)
      - Output/<job>/logs/pipeline.txt (human-readable)
      - Output/<job>/logs/stages/<stage>.log (JSONL)
      - Output/<job>/logs/summary.json
    """

    def __init__(self, *, job_dir: Path, job_id: str) -> None:
        self.job_dir = Path(job_dir)
        self.job_id = str(job_id)
        self.paths = job_log_paths(self.job_dir)
        ensure_dir(self.paths.root)
        ensure_dir(self.paths.stages_dir)
        ensure_dir(self.paths.ffmpeg_dir)

    def _append_line(self, path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")

    def event(self, *, stage: str, level: str, msg: str, **fields: Any) -> None:
        ev: dict[str, Any] = {
            "ts": time.time(),
            "job_id": self.job_id,
            "stage": str(stage),
            "level": str(level).lower(),
            "msg": str(msg),
            **fields,
        }
        # Reuse structlog redaction processor for string fields.
        ev = redact_event(None, None, ev)
        line = json.dumps(ev, ensure_ascii=False)
        self._append_line(self.paths.pipeline_jsonl, line)
        self._append_line(self.paths.stages_dir / f"{stage}.log", line)

        # human-readable breadcrumb
        self._append_line(
            self.paths.pipeline_txt,
            f"[{ev.get('level')}][{stage}] {msg}",
        )

    def write_summary(self, data: dict[str, Any]) -> None:
        atomic_write_text(
            self.paths.summary_json,
            json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False),
        )
