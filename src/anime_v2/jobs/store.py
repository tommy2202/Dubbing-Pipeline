from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from sqlitedict import SqliteDict  # type: ignore

from anime_v2.jobs.models import Job, JobState, now_utc


class JobStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _jobs(self) -> SqliteDict:
        # Open/close per operation (safe + avoids cross-thread SQLite handle issues)
        return SqliteDict(str(self.db_path), tablename="jobs", autocommit=True)

    def put(self, job: Job) -> None:
        with self._lock:
            with self._jobs() as db:
                db[job.id] = job.to_dict()

    def get(self, id: str) -> Job | None:
        with self._lock:
            with self._jobs() as db:
                raw = db.get(id)
        if raw is None:
            return None
        return Job.from_dict(raw)

    def update(self, id: str, **fields: Any) -> Job | None:
        with self._lock:
            with self._jobs() as db:
                raw = db.get(id)
                if raw is None:
                    return None
                raw = dict(raw)
                if "state" in fields and isinstance(fields["state"], JobState):
                    fields["state"] = fields["state"].value
                raw.update(fields)
                raw["updated_at"] = now_utc()
                db[id] = raw
        return Job.from_dict(raw)

    def list(self, limit: int = 100, state: str | None = None) -> list[Job]:
        with self._lock:
            with self._jobs() as db:
                items = list(db.items())

        jobs = [Job.from_dict(v) for _, v in items]
        if state:
            try:
                st = JobState(state)
                jobs = [j for j in jobs if j.state == st]
            except Exception:
                jobs = []
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def append_log(self, id: str, text: str) -> None:
        job = self.get(id)
        if job is None:
            return
        if not job.log_path:
            return
        path = Path(job.log_path)
        if path.exists() and path.is_dir():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(text.rstrip("\n") + "\n")

    def tail_log(self, id: str, n: int = 200) -> str:
        job = self.get(id)
        if job is None:
            return ""
        path = Path(job.log_path)
        if not path.exists():
            return ""
        # Simple read; logs are expected to be small per job.
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max(1, n) :]) + ("\n" if lines else "")

