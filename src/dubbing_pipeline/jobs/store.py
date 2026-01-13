from __future__ import annotations

import sqlite3
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any

from sqlitedict import SqliteDict  # type: ignore

from dubbing_pipeline.jobs.models import Job, JobState, now_utc


class JobStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Ensure core tables exist before any schema migrations that reference them.
        with suppress(Exception):
            with self._jobs() as _db:
                pass
        # Schema for grouped library browsing (indexed SQL table inside jobs.db).
        with suppress(Exception):
            self._init_library_schema()

    def _jobs(self) -> SqliteDict:
        # Open/close per operation (safe + avoids cross-thread SQLite handle issues)
        return SqliteDict(str(self.db_path), tablename="jobs", autocommit=True)

    def _idem(self) -> SqliteDict:
        return SqliteDict(str(self.db_path), tablename="idempotency", autocommit=True)

    def _presets(self) -> SqliteDict:
        return SqliteDict(str(self.db_path), tablename="presets", autocommit=True)

    def _projects(self) -> SqliteDict:
        return SqliteDict(str(self.db_path), tablename="projects", autocommit=True)

    def _uploads(self) -> SqliteDict:
        return SqliteDict(str(self.db_path), tablename="uploads", autocommit=True)

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path))
        con.row_factory = sqlite3.Row
        # Ensure FK enforcement when available (best-effort; does not break if disabled).
        with suppress(Exception):
            con.execute("PRAGMA foreign_keys = ON;")
        return con

    def _jobs_pk_col(self) -> str:
        """
        SqliteDict's backing table schema is implementation-defined.
        We discover the primary key column so we can reference jobs by FK.
        """
        con = self._conn()
        try:
            rows = con.execute("PRAGMA table_info(jobs);").fetchall()
            for r in rows:
                try:
                    if int(r["pk"] or 0) == 1:
                        return str(r["name"])
                except Exception:
                    continue
        finally:
            con.close()
        return "key"

    def _init_library_schema(self) -> None:
        """
        Create/migrate the SQL table used for indexed, grouped library browsing.
        Backwards-compatible: never rewrites the existing jobs table.
        """
        pk_col = self._jobs_pk_col()
        con = self._conn()
        try:
            con.execute(
                f"""
                CREATE TABLE IF NOT EXISTS job_library (
                  job_id TEXT PRIMARY KEY,
                  owner_user_id TEXT NOT NULL,
                  series_title TEXT,
                  series_slug TEXT,
                  season_number INTEGER,
                  episode_number INTEGER,
                  visibility TEXT NOT NULL DEFAULT 'private',
                  created_at TEXT,
                  updated_at TEXT,
                  FOREIGN KEY(job_id) REFERENCES jobs({pk_col}) ON DELETE CASCADE
                );
                """
            )

            # Best-effort, additive migrations (older DBs).
            cols = []
            with suppress(Exception):
                cols = [
                    str(r["name"])
                    for r in con.execute("PRAGMA table_info(job_library);").fetchall()
                ]
            want: dict[str, str] = {
                "owner_user_id": "TEXT",
                "series_title": "TEXT",
                "series_slug": "TEXT",
                "season_number": "INTEGER",
                "episode_number": "INTEGER",
                "visibility": "TEXT",
                "created_at": "TEXT",
                "updated_at": "TEXT",
            }
            for name, typ in want.items():
                if name in cols:
                    continue
                with suppress(Exception):
                    con.execute(f"ALTER TABLE job_library ADD COLUMN {name} {typ};")

            # Indexes for grouped browsing queries.
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_library_series_slug ON job_library(series_slug);"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_library_series_season_episode ON job_library(series_slug, season_number, episode_number);"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_library_owner_user_id ON job_library(owner_user_id);"
            )
            con.commit()
        finally:
            con.close()

    def _maybe_upsert_library_from_raw(self, job_id: str, raw: dict[str, Any]) -> None:
        """
        Best-effort denormalized index for library browsing.

        This is intentionally tolerant: legacy jobs may not have library fields yet.
        """
        try:
            from dubbing_pipeline.library.normalize import normalize_series_title, series_to_slug
        except Exception:
            return

        title_in = str(raw.get("series_title") or "")
        title = normalize_series_title(title_in)
        try:
            season = int(raw.get("season_number") or 0)
        except Exception:
            season = 0
        try:
            ep = int(raw.get("episode_number") or 0)
        except Exception:
            ep = 0

        # Only index jobs with complete library metadata.
        if not title or season < 1 or ep < 1:
            return

        slug = str(raw.get("series_slug") or "").strip()
        if not slug:
            slug = series_to_slug(title)
        if not slug:
            return

        owner = str(raw.get("owner_id") or raw.get("owner_user_id") or "").strip()
        if not owner:
            return

        vis = str(raw.get("visibility") or "private").strip().lower()
        if vis.startswith("visibility."):
            vis = vis.split(".", 1)[1]
        if vis not in {"private", "public"}:
            vis = "private"

        created_at = str(raw.get("created_at") or "").strip() or None
        updated_at = str(raw.get("updated_at") or "").strip() or None

        con = self._conn()
        try:
            con.execute(
                """
                INSERT INTO job_library (
                  job_id, owner_user_id, series_title, series_slug, season_number, episode_number,
                  visibility, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                  owner_user_id=excluded.owner_user_id,
                  series_title=excluded.series_title,
                  series_slug=excluded.series_slug,
                  season_number=excluded.season_number,
                  episode_number=excluded.episode_number,
                  visibility=excluded.visibility,
                  created_at=COALESCE(excluded.created_at, job_library.created_at),
                  updated_at=excluded.updated_at
                ;
                """,
                (str(job_id), owner, title, slug, int(season), int(ep), vis, created_at, updated_at),
            )
            con.commit()
        finally:
            con.close()

    def put(self, job: Job) -> None:
        with self._lock, self._jobs() as db:
            raw = job.to_dict()
            db[job.id] = raw
            with suppress(Exception):
                self._maybe_upsert_library_from_raw(job.id, raw)

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
            with suppress(Exception):
                self._maybe_upsert_library_from_raw(id, raw)
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

    def delete_job(self, id: str) -> None:
        if not id:
            return
        with self._lock:
            with self._jobs() as db, suppress(Exception):
                del db[str(id)]
            with suppress(Exception):
                con = self._conn()
                try:
                    con.execute("DELETE FROM job_library WHERE job_id = ?;", (str(id),))
                    con.commit()
                finally:
                    con.close()

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
        if not job.log_path:
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

    # --- resumable uploads (web/mobile) ---
    def put_upload(self, upload_id: str, rec: dict[str, Any]) -> dict[str, Any]:
        if not upload_id:
            raise ValueError("upload_id required")
        with self._lock, self._uploads() as db:
            db[str(upload_id)] = dict(rec)
            return dict(rec)

    def get_upload(self, upload_id: str) -> dict[str, Any] | None:
        if not upload_id:
            return None
        with self._lock, self._uploads() as db:
            v = db.get(str(upload_id))
        return dict(v) if isinstance(v, dict) else None

    def update_upload(self, upload_id: str, **fields: Any) -> dict[str, Any] | None:
        if not upload_id:
            return None
        with self._lock, self._uploads() as db:
            raw = db.get(str(upload_id))
            if not isinstance(raw, dict):
                return None
            raw = dict(raw)
            raw.update(fields)
            db[str(upload_id)] = raw
            return dict(raw)

    def delete_upload(self, upload_id: str) -> None:
        with self._lock, self._uploads() as db, suppress(Exception):
            del db[str(upload_id)]
