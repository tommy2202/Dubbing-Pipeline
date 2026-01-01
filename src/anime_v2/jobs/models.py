from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class JobState(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


def now_utc() -> str:
    return datetime.now(tz=UTC).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass(slots=True)
class Job:
    id: str
    video_path: str
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

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Job":
        dd = dict(d)
        st = dd["state"]
        if isinstance(st, str) and st.startswith("JobState."):
            st = st.split(".", 1)[1]
        dd["state"] = JobState(st)
        return cls(**dd)

