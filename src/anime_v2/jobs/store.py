from __future__ import annotations

import threading
from contextlib import suppress
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

    def _idem(self) -> SqliteDict:
        return SqliteDict(str(self.db_path), tablename="idempotency", autocommit=True)

    def _presets(self) -> SqliteDict:
        return SqliteDict(str(self.db_path), tablename="presets", autocommit=True)

    def _projects(self) -> SqliteDict:
        return SqliteDict(str(self.db_path), tablename="projects", autocommit=True)

    def put(self, job: Job) -> None:
        with self._lock, self._jobs() as db:
            db[job.id] = job.to_dict()

    def get(self, id: str) -> Job | None:
        with self._lock, self._jobs() as db:
            raw = db.get(id)
        if raw is None:
            return None
        return Job.from_dict(raw)

    def update(self, id: str, **fields: Any) -> Job | None:
        with self._lock, self._jobs() as db:
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
        with self._lock, self._jobs() as db:
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
        with self._lock, path.open("a", encoding="utf-8") as f:
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

    def get_idempotency(self, key: str) -> tuple[str, float] | None:
        if not key:
            return None
        with self._lock, self._idem() as db:
            v = db.get(key)
        if not isinstance(v, dict):
            return None
        jid = str(v.get("job_id") or "")
        ts = float(v.get("ts") or 0.0)
        if not jid:
            return None
        return jid, ts

    def put_idempotency(self, key: str, job_id: str) -> None:
        if not key:
            return
        with self._lock, self._idem() as db:
            db[key] = {"job_id": str(job_id), "ts": __import__("time").time()}

    # --- presets ---
    def list_presets(self, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._presets() as db:
            items = list(db.values())
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            if owner_id and str(it.get("owner_id") or "") != str(owner_id):
                continue
            out.append(dict(it))
        out.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
        return out

    def get_preset(self, preset_id: str) -> dict[str, Any] | None:
        with self._lock, self._presets() as db:
            v = db.get(str(preset_id))
        return dict(v) if isinstance(v, dict) else None

    def put_preset(self, preset: dict[str, Any]) -> dict[str, Any]:
        pid = str(preset.get("id") or "")
        if not pid:
            raise ValueError("preset.id required")
        with self._lock, self._presets() as db:
            db[pid] = dict(preset)
        return dict(preset)

    def delete_preset(self, preset_id: str) -> None:
        with self._lock, self._presets() as db, suppress(Exception):
            del db[str(preset_id)]

    # --- projects ---
    def list_projects(self, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._projects() as db:
            items = list(db.values())
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            if owner_id and str(it.get("owner_id") or "") != str(owner_id):
                continue
            out.append(dict(it))
        out.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
        return out

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._lock, self._projects() as db:
            v = db.get(str(project_id))
        return dict(v) if isinstance(v, dict) else None

    def put_project(self, project: dict[str, Any]) -> dict[str, Any]:
        pid = str(project.get("id") or "")
        if not pid:
            raise ValueError("project.id required")
        with self._lock, self._projects() as db:
            db[pid] = dict(project)
        return dict(project)

    def delete_project(self, project_id: str) -> None:
        with self._lock, self._projects() as db, suppress(Exception):
            del db[str(project_id)]
