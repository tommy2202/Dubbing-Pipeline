from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class JobState(str, Enum):
    QUEUED = "QUEUED"
    PAUSED = "PAUSED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class Visibility(str, Enum):
    private = "private"
    public = "public"


def now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass(slots=True)
class Job:
    id: str
    owner_id: str
    video_path: str
    duration_s: float
    mode: str
    device: str
    src_lang: str
    tgt_lang: str
    created_at: str
    updated_at: str
    state: JobState
    progress: float
    message: str
    output_mkv: str
    output_srt: str
    work_dir: str
    log_path: str
    error: str | None = None
    request_id: str = ""
    runtime: dict[str, Any] = field(default_factory=dict)

    # Grouped library browsing metadata (optional for legacy jobs).
    series_title: str = ""
    series_slug: str = ""
    season_number: int = 0
    episode_number: int = 0
    visibility: Visibility = Visibility.private

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        d["visibility"] = self.visibility.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Job:
        dd = dict(d)
        # Backwards-compatible defaults for older persisted jobs.
        dd.setdefault("owner_id", "")
        dd.setdefault("duration_s", 0.0)
        dd.setdefault("request_id", "")
        dd.setdefault("error", None)
        dd.setdefault("runtime", {})
        dd.setdefault("series_title", "")
        dd.setdefault("series_slug", "")
        dd.setdefault("season_number", 0)
        dd.setdefault("episode_number", 0)
        dd.setdefault("visibility", "private")
        st = dd["state"]
        if isinstance(st, str) and st.startswith("JobState."):
            st = st.split(".", 1)[1]
        dd["state"] = JobState(st)
        vis = dd.get("visibility")
        if isinstance(vis, str) and vis.startswith("Visibility."):
            vis = vis.split(".", 1)[1]
        try:
            dd["visibility"] = Visibility(str(vis))
        except Exception:
            dd["visibility"] = Visibility.private
        return cls(**dd)
