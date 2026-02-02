from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from sqlitedict import SqliteDict  # type: ignore

from dubbing_pipeline.jobs.models import Job, JobState, now_utc
from dubbing_pipeline.utils.locks import file_lock


class JobStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._lock_path = self.db_path.with_suffix(self.db_path.suffix + ".lock")
        # Ensure core tables exist before any schema migrations that reference them.
        with suppress(Exception):
            with self._jobs() as _db:
                pass
        # Schema for grouped library browsing (indexed SQL table inside jobs.db).
        with suppress(Exception):
            self._init_library_schema()
        # Schema for persistent character voice + per-job speaker mapping.
        with suppress(Exception):
            self._init_voice_schema()
        # Schema for user view history (library continue panel).
        with suppress(Exception):
            self._init_view_history_schema()
        # Schema for per-user storage accounting.
        with suppress(Exception):
            self._init_storage_schema()
        # Schema for per-user quota overrides (admin).
        with suppress(Exception):
            self._init_quota_schema()
        # Schema for library reports (moderation).
        with suppress(Exception):
            self._init_reports_schema()
        # Schema for QA review annotations (per-segment).
        with suppress(Exception):
            self._init_qa_schema()
        # Schema for persistent voice profiles + aliases.
        with suppress(Exception):
            self._init_voice_profile_schema()
        # Schema for series glossaries (deterministic rules).
        with suppress(Exception):
            self._init_glossary_schema()
        # Schema for pronunciation dictionary (per-language).
        with suppress(Exception):
            self._init_pronunciation_schema()

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

    def _write_lock(self):
        return file_lock(self._lock_path)

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
        with self._write_lock():
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

    def _init_voice_schema(self) -> None:
        """
        Create/migrate tables for persistent character voices and per-job speaker mapping.

        Tables:
        - character_voice: series_slug, character_slug, display_name, ref_path, updated_at, created_by
        - speaker_mapping: job_id, speaker_id, character_slug, confidence, locked
        """
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS character_voice (
                      series_slug TEXT NOT NULL,
                      character_slug TEXT NOT NULL,
                      display_name TEXT,
                      ref_path TEXT,
                      updated_at TEXT,
                      created_by TEXT,
                      PRIMARY KEY(series_slug, character_slug)
                    );
                    """
                )
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS speaker_mapping (
                      job_id TEXT NOT NULL,
                      speaker_id TEXT NOT NULL,
                      character_slug TEXT NOT NULL,
                      confidence REAL NOT NULL DEFAULT 1.0,
                      locked INTEGER NOT NULL DEFAULT 1,
                      updated_at TEXT,
                      created_by TEXT,
                      PRIMARY KEY(job_id, speaker_id)
                    );
                    """
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_character_voice_series_slug ON character_voice(series_slug);"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_speaker_mapping_job_id ON speaker_mapping(job_id);"
                )
                con.commit()
            finally:
                con.close()

    def _init_view_history_schema(self) -> None:
        """
        Create/migrate the SQL table used for minimal view history (continue panel).
        """
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS view_history (
                      user_id TEXT NOT NULL,
                      series_slug TEXT NOT NULL,
                      season_number INTEGER NOT NULL,
                      episode_number INTEGER NOT NULL,
                      job_id TEXT,
                      last_opened_at REAL NOT NULL,
                      PRIMARY KEY(user_id, series_slug, season_number, episode_number)
                    );
                    """
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_view_history_user_last ON view_history(user_id, last_opened_at);"
                )
                con.commit()
            finally:
                con.close()

    def _init_storage_schema(self) -> None:
        """
        Create/migrate tables for per-user storage accounting.

        Tables:
        - user_storage: user_id, bytes, updated_at
        - job_storage: job_id, user_id, bytes, updated_at
        - upload_storage: upload_id, user_id, bytes, updated_at
        """
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_storage (
                      user_id TEXT PRIMARY KEY,
                      bytes INTEGER NOT NULL DEFAULT 0,
                      updated_at REAL
                    );
                    """
                )
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS job_storage (
                      job_id TEXT PRIMARY KEY,
                      user_id TEXT NOT NULL,
                      bytes INTEGER NOT NULL DEFAULT 0,
                      updated_at REAL
                    );
                    """
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_job_storage_user_id ON job_storage(user_id);"
                )
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS upload_storage (
                      upload_id TEXT PRIMARY KEY,
                      user_id TEXT NOT NULL,
                      bytes INTEGER NOT NULL DEFAULT 0,
                      updated_at REAL
                    );
                    """
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_upload_storage_user_id ON upload_storage(user_id);"
                )
                con.commit()
            finally:
                con.close()

    def _init_quota_schema(self) -> None:
        """
        Create/migrate table for per-user quota overrides.
        """
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_quotas (
                      user_id TEXT PRIMARY KEY,
                      max_upload_bytes INTEGER,
                      jobs_per_day INTEGER,
                      max_concurrent_jobs INTEGER,
                      max_storage_bytes INTEGER,
                      updated_at REAL,
                      updated_by TEXT
                    );
                    """
                )
                con.commit()
            finally:
                con.close()

    def _init_reports_schema(self) -> None:
        """
        Create/migrate table for library moderation reports.
        """
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS library_reports (
                      id TEXT PRIMARY KEY,
                      job_id TEXT,
                      series_slug TEXT,
                      season_number INTEGER,
                      episode_number INTEGER,
                      reporter_id TEXT NOT NULL,
                      owner_id TEXT,
                      reason TEXT,
                      created_at REAL NOT NULL,
                      status TEXT NOT NULL DEFAULT 'open',
                      notified INTEGER NOT NULL DEFAULT 0,
                      notify_error TEXT
                    );
                    """
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_library_reports_status_created ON library_reports(status, created_at DESC);"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_library_reports_job_id ON library_reports(job_id);"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_library_reports_reporter_id ON library_reports(reporter_id);"
                )
                con.commit()
            finally:
                con.close()

    def _init_qa_schema(self) -> None:
        """
        Create/migrate tables for per-segment QA reviews.
        """
        with self._write_lock():
            pk_col = self._jobs_pk_col()
            con = self._conn()
            try:
                con.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS qa_reviews (
                      id TEXT PRIMARY KEY,
                      job_id TEXT NOT NULL,
                      segment_id INTEGER NOT NULL,
                      status TEXT DEFAULT 'pending',
                      notes TEXT,
                      edited_text TEXT,
                      pronunciation_overrides TEXT,
                      glossary_used TEXT,
                      created_by TEXT,
                      created_at REAL,
                      updated_at REAL,
                      FOREIGN KEY(job_id) REFERENCES jobs({pk_col}) ON DELETE CASCADE
                    );
                    """
                )
                cols = []
                with suppress(Exception):
                    cols = [
                        str(r["name"])
                        for r in con.execute("PRAGMA table_info(qa_reviews);").fetchall()
                    ]
                want: dict[str, str] = {
                    "id": "TEXT",
                    "job_id": "TEXT",
                    "segment_id": "INTEGER",
                    "status": "TEXT DEFAULT 'pending'",
                    "notes": "TEXT",
                    "edited_text": "TEXT",
                    "pronunciation_overrides": "TEXT",
                    "glossary_used": "TEXT",
                    "created_by": "TEXT",
                    "created_at": "REAL",
                    "updated_at": "REAL",
                }
                for name, typ in want.items():
                    if name in cols:
                        continue
                    with suppress(Exception):
                        con.execute(f"ALTER TABLE qa_reviews ADD COLUMN {name} {typ};")
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_qa_reviews_job_id ON qa_reviews(job_id);"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_qa_reviews_job_segment ON qa_reviews(job_id, segment_id);"
                )
                con.commit()
            finally:
                con.close()

    def _init_voice_profile_schema(self) -> None:
        """
        Create/migrate tables for voice profiles and alias mappings.
        """
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS voice_profiles (
                      id TEXT PRIMARY KEY,
                      display_name TEXT,
                      created_by TEXT,
                      created_at REAL,
                      scope TEXT DEFAULT 'private',
                      series_lock TEXT,
                      source_type TEXT DEFAULT 'unknown',
                      export_allowed INTEGER DEFAULT 0,
                      share_allowed INTEGER DEFAULT 0,
                      reuse_allowed INTEGER,
                      expires_at REAL,
                      embedding_vector BLOB,
                      embedding_model_id TEXT,
                      metadata_json TEXT
                    );
                    """
                )
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS voice_profile_aliases (
                      id TEXT PRIMARY KEY,
                      voice_profile_id TEXT NOT NULL,
                      alias_of_voice_profile_id TEXT NOT NULL,
                      confidence REAL NOT NULL DEFAULT 0.0,
                      approved_by_admin INTEGER NOT NULL DEFAULT 0,
                      approved_at REAL,
                      FOREIGN KEY(voice_profile_id) REFERENCES voice_profiles(id) ON DELETE CASCADE,
                      FOREIGN KEY(alias_of_voice_profile_id) REFERENCES voice_profiles(id) ON DELETE CASCADE
                    );
                    """
                )
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS voice_profile_suggestions (
                      id TEXT PRIMARY KEY,
                      voice_profile_id TEXT NOT NULL,
                      suggested_profile_id TEXT NOT NULL,
                      similarity REAL NOT NULL DEFAULT 0.0,
                      status TEXT NOT NULL DEFAULT 'pending',
                      created_by TEXT,
                      created_at REAL,
                      updated_at REAL,
                      FOREIGN KEY(voice_profile_id) REFERENCES voice_profiles(id) ON DELETE CASCADE,
                      FOREIGN KEY(suggested_profile_id) REFERENCES voice_profiles(id) ON DELETE CASCADE
                    );
                    """
                )
                cols = []
                with suppress(Exception):
                    cols = [
                        str(r["name"])
                        for r in con.execute("PRAGMA table_info(voice_profiles);").fetchall()
                    ]
                want_profiles: dict[str, str] = {
                    "id": "TEXT",
                    "display_name": "TEXT",
                    "created_by": "TEXT",
                    "created_at": "REAL",
                    "scope": "TEXT DEFAULT 'private'",
                    "series_lock": "TEXT",
                    "source_type": "TEXT DEFAULT 'unknown'",
                    "export_allowed": "INTEGER DEFAULT 0",
                    "share_allowed": "INTEGER DEFAULT 0",
                    "reuse_allowed": "INTEGER",
                    "expires_at": "REAL",
                    "embedding_vector": "BLOB",
                    "embedding_model_id": "TEXT",
                    "metadata_json": "TEXT",
                }
                for name, typ in want_profiles.items():
                    if name in cols:
                        continue
                    with suppress(Exception):
                        con.execute(f"ALTER TABLE voice_profiles ADD COLUMN {name} {typ};")

                cols_alias = []
                with suppress(Exception):
                    cols_alias = [
                        str(r["name"])
                        for r in con.execute("PRAGMA table_info(voice_profile_aliases);").fetchall()
                    ]
                want_alias: dict[str, str] = {
                    "id": "TEXT",
                    "voice_profile_id": "TEXT",
                    "alias_of_voice_profile_id": "TEXT",
                    "confidence": "REAL DEFAULT 0.0",
                    "approved_by_admin": "INTEGER DEFAULT 0",
                    "approved_at": "REAL",
                }
                for name, typ in want_alias.items():
                    if name in cols_alias:
                        continue
                    with suppress(Exception):
                        con.execute(
                            f"ALTER TABLE voice_profile_aliases ADD COLUMN {name} {typ};"
                        )

                cols_suggest = []
                with suppress(Exception):
                    cols_suggest = [
                        str(r["name"])
                        for r in con.execute("PRAGMA table_info(voice_profile_suggestions);").fetchall()
                    ]
                want_suggest: dict[str, str] = {
                    "id": "TEXT",
                    "voice_profile_id": "TEXT",
                    "suggested_profile_id": "TEXT",
                    "similarity": "REAL DEFAULT 0.0",
                    "status": "TEXT DEFAULT 'pending'",
                    "created_by": "TEXT",
                    "created_at": "REAL",
                    "updated_at": "REAL",
                }
                for name, typ in want_suggest.items():
                    if name in cols_suggest:
                        continue
                    with suppress(Exception):
                        con.execute(
                            f"ALTER TABLE voice_profile_suggestions ADD COLUMN {name} {typ};"
                        )

                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_voice_profiles_series_lock ON voice_profiles(series_lock);"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_voice_profiles_embedding_model ON voice_profiles(embedding_model_id);"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_voice_profile_aliases_voice_id ON voice_profile_aliases(voice_profile_id);"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_voice_profile_aliases_alias_id ON voice_profile_aliases(alias_of_voice_profile_id);"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_voice_profile_suggest_voice_id ON voice_profile_suggestions(voice_profile_id);"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_voice_profile_suggest_suggested_id ON voice_profile_suggestions(suggested_profile_id);"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_voice_profile_suggest_status ON voice_profile_suggestions(status);"
                )
                con.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_voice_profile_suggest_pair
                    ON voice_profile_suggestions(voice_profile_id, suggested_profile_id);
                    """
                )

                # Default policies (best-effort; only apply when reuse_allowed is NULL).
                con.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS trg_voice_profiles_reuse_default
                    AFTER INSERT ON voice_profiles
                    FOR EACH ROW
                    WHEN NEW.reuse_allowed IS NULL
                    BEGIN
                      UPDATE voice_profiles
                      SET reuse_allowed = CASE
                        WHEN NEW.source_type = 'user_upload' THEN 1
                        WHEN NEW.source_type = 'extracted_from_media' THEN 0
                        ELSE 0
                      END
                      WHERE id = NEW.id;
                    END;
                    """
                )
                con.commit()
            finally:
                con.close()

    def _init_glossary_schema(self) -> None:
        """
        Create/migrate table for deterministic glossaries.
        """
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS glossaries (
                      id TEXT PRIMARY KEY,
                      name TEXT NOT NULL,
                      language_pair TEXT NOT NULL,
                      series_slug TEXT,
                      priority INTEGER DEFAULT 0,
                      enabled INTEGER DEFAULT 1,
                      rules_json TEXT NOT NULL,
                      created_at REAL,
                      updated_at REAL
                    );
                    """
                )
                cols = []
                with suppress(Exception):
                    cols = [
                        str(r["name"])
                        for r in con.execute("PRAGMA table_info(glossaries);").fetchall()
                    ]
                want: dict[str, str] = {
                    "id": "TEXT",
                    "name": "TEXT",
                    "language_pair": "TEXT",
                    "series_slug": "TEXT",
                    "priority": "INTEGER DEFAULT 0",
                    "enabled": "INTEGER DEFAULT 1",
                    "rules_json": "TEXT",
                    "created_at": "REAL",
                    "updated_at": "REAL",
                }
                for name, typ in want.items():
                    if name in cols:
                        continue
                    with suppress(Exception):
                        con.execute(f"ALTER TABLE glossaries ADD COLUMN {name} {typ};")
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_glossaries_lang_series ON glossaries(language_pair, series_slug, priority DESC);"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_glossaries_enabled ON glossaries(enabled);"
                )
                con.commit()
            finally:
                con.close()

    def _init_pronunciation_schema(self) -> None:
        """
        Create/migrate table for pronunciation dictionary entries.
        """
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pronunciation_dict (
                      id TEXT PRIMARY KEY,
                      lang TEXT NOT NULL,
                      term TEXT NOT NULL,
                      ipa_or_phoneme TEXT NOT NULL,
                      example TEXT,
                      created_by TEXT,
                      created_at REAL
                    );
                    """
                )
                cols = []
                with suppress(Exception):
                    cols = [
                        str(r["name"])
                        for r in con.execute("PRAGMA table_info(pronunciation_dict);").fetchall()
                    ]
                want: dict[str, str] = {
                    "id": "TEXT",
                    "lang": "TEXT",
                    "term": "TEXT",
                    "ipa_or_phoneme": "TEXT",
                    "example": "TEXT",
                    "created_by": "TEXT",
                    "created_at": "REAL",
                }
                for name, typ in want.items():
                    if name in cols:
                        continue
                    with suppress(Exception):
                        con.execute(
                            f"ALTER TABLE pronunciation_dict ADD COLUMN {name} {typ};"
                        )
                con.commit()
            finally:
                con.close()

    def record_view(
        self,
        *,
        user_id: str,
        series_slug: str,
        season_number: int,
        episode_number: int,
        job_id: str | None = None,
        opened_at: float | None = None,
    ) -> None:
        uid = str(user_id or "").strip()
        slug = str(series_slug or "").strip()
        if not uid or not slug:
            return
        try:
            season = int(season_number)
            episode = int(episode_number)
        except Exception:
            return
        if season < 1 or episode < 1:
            return
        ts = float(opened_at or time.time())
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    INSERT INTO view_history (
                      user_id, series_slug, season_number, episode_number, job_id, last_opened_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, series_slug, season_number, episode_number)
                    DO UPDATE SET job_id=excluded.job_id, last_opened_at=excluded.last_opened_at;
                    """,
                    (
                        uid,
                        slug,
                        int(season),
                        int(episode),
                        str(job_id) if job_id else None,
                        float(ts),
                    ),
                )
                con.commit()
            finally:
                con.close()

    def list_view_history(self, *, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        uid = str(user_id or "").strip()
        if not uid:
            return []
        lim = max(1, min(100, int(limit)))
        con = self._conn()
        try:
            rows = con.execute(
                """
                SELECT user_id, series_slug, season_number, episode_number, job_id, last_opened_at
                FROM view_history
                WHERE user_id = ?
                ORDER BY last_opened_at DESC
                LIMIT ?;
                """,
                (uid, lim),
            ).fetchall()
            return [
                {
                    "user_id": str(r["user_id"]),
                    "series_slug": str(r["series_slug"]),
                    "season_number": int(r["season_number"] or 0),
                    "episode_number": int(r["episode_number"] or 0),
                    "job_id": str(r["job_id"] or "") or None,
                    "last_opened_at": float(r["last_opened_at"] or 0.0),
                }
                for r in rows
            ]
        finally:
            con.close()

    # --- per-user storage accounting ---
    def get_user_storage_bytes(self, user_id: str) -> int:
        uid = str(user_id or "").strip()
        if not uid:
            return 0
        con = self._conn()
        try:
            row = con.execute("SELECT bytes FROM user_storage WHERE user_id = ?;", (uid,)).fetchone()
            if row is None:
                return 0
            return max(0, int(row["bytes"] or 0))
        finally:
            con.close()

    def list_user_storage(self, *, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        lim = max(1, min(1000, int(limit)))
        off = max(0, int(offset))
        con = self._conn()
        try:
            rows = con.execute(
                """
                SELECT user_id, bytes, updated_at
                FROM user_storage
                ORDER BY bytes DESC
                LIMIT ? OFFSET ?;
                """,
                (lim, off),
            ).fetchall()
            return [
                {
                    "user_id": str(r["user_id"]),
                    "bytes": max(0, int(r["bytes"] or 0)),
                    "updated_at": float(r["updated_at"] or 0.0),
                }
                for r in rows
            ]
        finally:
            con.close()

    def set_job_storage_bytes(self, job_id: str, *, user_id: str, bytes_count: int) -> int:
        job_id = str(job_id or "").strip()
        uid = str(user_id or "").strip()
        if not job_id or not uid:
            return 0
        new_bytes = max(0, int(bytes_count))
        now = float(time.time())
        with self._write_lock():
            con = self._conn()
            try:
                row = con.execute(
                    "SELECT user_id, bytes FROM job_storage WHERE job_id = ?;",
                    (job_id,),
                ).fetchone()
                prev_bytes = 0
                prev_user = uid
                if row is not None:
                    prev_user = str(row["user_id"] or "")
                    prev_bytes = max(0, int(row["bytes"] or 0))

                # If ownership changed (unexpected), subtract from prior user.
                if prev_user and prev_user != uid:
                    prow = con.execute(
                        "SELECT bytes FROM user_storage WHERE user_id = ?;",
                        (prev_user,),
                    ).fetchone()
                    pbytes = max(0, int(prow["bytes"] or 0)) if prow is not None else 0
                    con.execute(
                        "INSERT INTO user_storage (user_id, bytes, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(user_id) DO UPDATE SET bytes=excluded.bytes, updated_at=excluded.updated_at;",
                        (prev_user, max(0, pbytes - prev_bytes), now),
                    )
                    prev_bytes = 0

                delta = new_bytes - prev_bytes
                urow = con.execute(
                    "SELECT bytes FROM user_storage WHERE user_id = ?;",
                    (uid,),
                ).fetchone()
                ubytes = max(0, int(urow["bytes"] or 0)) if urow is not None else 0
                con.execute(
                    "INSERT INTO user_storage (user_id, bytes, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET bytes=excluded.bytes, updated_at=excluded.updated_at;",
                    (uid, max(0, ubytes + delta), now),
                )
                con.execute(
                    """
                    INSERT INTO job_storage (job_id, user_id, bytes, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                      user_id=excluded.user_id,
                      bytes=excluded.bytes,
                      updated_at=excluded.updated_at;
                    """,
                    (job_id, uid, new_bytes, now),
                )
                con.commit()
            finally:
                con.close()
        return new_bytes

    def delete_job_storage(self, job_id: str) -> int:
        job_id = str(job_id or "").strip()
        if not job_id:
            return 0
        removed = 0
        with self._write_lock():
            con = self._conn()
            try:
                row = con.execute(
                    "SELECT user_id, bytes FROM job_storage WHERE job_id = ?;",
                    (job_id,),
                ).fetchone()
                if row is None:
                    return 0
                uid = str(row["user_id"] or "")
                removed = max(0, int(row["bytes"] or 0))
                con.execute("DELETE FROM job_storage WHERE job_id = ?;", (job_id,))
                if uid:
                    urow = con.execute(
                        "SELECT bytes FROM user_storage WHERE user_id = ?;",
                        (uid,),
                    ).fetchone()
                    ubytes = max(0, int(urow["bytes"] or 0)) if urow is not None else 0
                    con.execute(
                        "INSERT INTO user_storage (user_id, bytes, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(user_id) DO UPDATE SET bytes=excluded.bytes, updated_at=excluded.updated_at;",
                        (uid, max(0, ubytes - removed), float(time.time())),
                    )
                con.commit()
            finally:
                con.close()
        return removed

    def set_upload_storage_bytes(self, upload_id: str, *, user_id: str, bytes_count: int) -> int:
        upload_id = str(upload_id or "").strip()
        uid = str(user_id or "").strip()
        if not upload_id or not uid:
            return 0
        new_bytes = max(0, int(bytes_count))
        now = float(time.time())
        with self._write_lock():
            con = self._conn()
            try:
                row = con.execute(
                    "SELECT user_id, bytes FROM upload_storage WHERE upload_id = ?;",
                    (upload_id,),
                ).fetchone()
                prev_bytes = 0
                prev_user = uid
                if row is not None:
                    prev_user = str(row["user_id"] or "")
                    prev_bytes = max(0, int(row["bytes"] or 0))

                # If ownership changed (unexpected), subtract from prior user.
                if prev_user and prev_user != uid:
                    prow = con.execute(
                        "SELECT bytes FROM user_storage WHERE user_id = ?;",
                        (prev_user,),
                    ).fetchone()
                    pbytes = max(0, int(prow["bytes"] or 0)) if prow is not None else 0
                    con.execute(
                        "INSERT INTO user_storage (user_id, bytes, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(user_id) DO UPDATE SET bytes=excluded.bytes, updated_at=excluded.updated_at;",
                        (prev_user, max(0, pbytes - prev_bytes), now),
                    )
                    prev_bytes = 0

                delta = new_bytes - prev_bytes
                urow = con.execute(
                    "SELECT bytes FROM user_storage WHERE user_id = ?;",
                    (uid,),
                ).fetchone()
                ubytes = max(0, int(urow["bytes"] or 0)) if urow is not None else 0
                con.execute(
                    "INSERT INTO user_storage (user_id, bytes, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET bytes=excluded.bytes, updated_at=excluded.updated_at;",
                    (uid, max(0, ubytes + delta), now),
                )
                con.execute(
                    """
                    INSERT INTO upload_storage (upload_id, user_id, bytes, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(upload_id) DO UPDATE SET
                      user_id=excluded.user_id,
                      bytes=excluded.bytes,
                      updated_at=excluded.updated_at;
                    """,
                    (upload_id, uid, new_bytes, now),
                )
                con.commit()
            finally:
                con.close()
        return new_bytes

    def delete_upload_storage(self, upload_id: str) -> int:
        upload_id = str(upload_id or "").strip()
        if not upload_id:
            return 0
        removed = 0
        with self._write_lock():
            con = self._conn()
            try:
                row = con.execute(
                    "SELECT user_id, bytes FROM upload_storage WHERE upload_id = ?;",
                    (upload_id,),
                ).fetchone()
                if row is None:
                    return 0
                uid = str(row["user_id"] or "")
                removed = max(0, int(row["bytes"] or 0))
                con.execute("DELETE FROM upload_storage WHERE upload_id = ?;", (upload_id,))
                if uid:
                    urow = con.execute(
                        "SELECT bytes FROM user_storage WHERE user_id = ?;",
                        (uid,),
                    ).fetchone()
                    ubytes = max(0, int(urow["bytes"] or 0)) if urow is not None else 0
                    con.execute(
                        "INSERT INTO user_storage (user_id, bytes, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(user_id) DO UPDATE SET bytes=excluded.bytes, updated_at=excluded.updated_at;",
                        (uid, max(0, ubytes - removed), float(time.time())),
                    )
                con.commit()
            finally:
                con.close()
        return removed

    def replace_storage_accounting(
        self,
        *,
        job_entries: list[tuple[str, str, int]] | None = None,
        upload_entries: list[tuple[str, str, int]] | None = None,
    ) -> None:
        jobs_in = job_entries or []
        uploads_in = upload_entries or []
        job_map: dict[str, tuple[str, int]] = {}
        for job_id, user_id, bytes_count in jobs_in:
            jid = str(job_id or "").strip()
            uid = str(user_id or "").strip()
            if not jid or not uid:
                continue
            job_map[jid] = (uid, max(0, int(bytes_count)))
        upload_map: dict[str, tuple[str, int]] = {}
        for upload_id, user_id, bytes_count in uploads_in:
            uid = str(user_id or "").strip()
            upid = str(upload_id or "").strip()
            if not upid or not uid:
                continue
            upload_map[upid] = (uid, max(0, int(bytes_count)))
        totals: dict[str, int] = {}
        now = float(time.time())
        with self._write_lock():
            con = self._conn()
            try:
                con.execute("BEGIN IMMEDIATE;")
                con.execute("DELETE FROM user_storage;")
                con.execute("DELETE FROM job_storage;")
                con.execute("DELETE FROM upload_storage;")
                for jid, (uid, bytes_count) in job_map.items():
                    totals[uid] = int(totals.get(uid, 0)) + int(bytes_count)
                    con.execute(
                        """
                        INSERT INTO job_storage (job_id, user_id, bytes, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(job_id) DO UPDATE SET
                          user_id=excluded.user_id,
                          bytes=excluded.bytes,
                          updated_at=excluded.updated_at;
                        """,
                        (jid, uid, int(bytes_count), now),
                    )
                for upid, (uid, bytes_count) in upload_map.items():
                    totals[uid] = int(totals.get(uid, 0)) + int(bytes_count)
                    con.execute(
                        """
                        INSERT INTO upload_storage (upload_id, user_id, bytes, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(upload_id) DO UPDATE SET
                          user_id=excluded.user_id,
                          bytes=excluded.bytes,
                          updated_at=excluded.updated_at;
                        """,
                        (upid, uid, int(bytes_count), now),
                    )
                for uid, bytes_count in totals.items():
                    con.execute(
                        "INSERT INTO user_storage (user_id, bytes, updated_at) VALUES (?, ?, ?);",
                        (uid, max(0, int(bytes_count)), now),
                    )
                con.commit()
            finally:
                with suppress(Exception):
                    if con.in_transaction:
                        con.rollback()
                con.close()

    # --- per-user quota overrides ---
    def get_user_quota(self, user_id: str) -> dict[str, int | None]:
        uid = str(user_id or "").strip()
        if not uid:
            return {}
        con = self._conn()
        try:
            row = con.execute(
                """
                SELECT max_upload_bytes, jobs_per_day, max_concurrent_jobs, max_storage_bytes
                FROM user_quotas
                WHERE user_id = ?;
                """,
                (uid,),
            ).fetchone()
            if row is None:
                return {}
            return {
                "max_upload_bytes": (int(row["max_upload_bytes"]) if row["max_upload_bytes"] is not None else None),
                "jobs_per_day": (int(row["jobs_per_day"]) if row["jobs_per_day"] is not None else None),
                "max_concurrent_jobs": (
                    int(row["max_concurrent_jobs"]) if row["max_concurrent_jobs"] is not None else None
                ),
                "max_storage_bytes": (
                    int(row["max_storage_bytes"]) if row["max_storage_bytes"] is not None else None
                ),
            }
        finally:
            con.close()

    def upsert_user_quota(
        self,
        user_id: str,
        *,
        max_upload_bytes: int | None,
        jobs_per_day: int | None,
        max_concurrent_jobs: int | None,
        max_storage_bytes: int | None,
        updated_by: str = "",
    ) -> dict[str, int | None]:
        uid = str(user_id or "").strip()
        if not uid:
            return {}
        if all(v is None for v in (max_upload_bytes, jobs_per_day, max_concurrent_jobs, max_storage_bytes)):
            with self._write_lock():
                con = self._conn()
                try:
                    con.execute("DELETE FROM user_quotas WHERE user_id = ?;", (uid,))
                    con.commit()
                finally:
                    con.close()
            return {}
        now = float(time.time())
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    INSERT INTO user_quotas (
                      user_id, max_upload_bytes, jobs_per_day, max_concurrent_jobs, max_storage_bytes,
                      updated_at, updated_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                      max_upload_bytes=excluded.max_upload_bytes,
                      jobs_per_day=excluded.jobs_per_day,
                      max_concurrent_jobs=excluded.max_concurrent_jobs,
                      max_storage_bytes=excluded.max_storage_bytes,
                      updated_at=excluded.updated_at,
                      updated_by=excluded.updated_by;
                    """,
                    (
                        uid,
                        (int(max_upload_bytes) if max_upload_bytes is not None else None),
                        (int(jobs_per_day) if jobs_per_day is not None else None),
                        (int(max_concurrent_jobs) if max_concurrent_jobs is not None else None),
                        (int(max_storage_bytes) if max_storage_bytes is not None else None),
                        now,
                        str(updated_by or ""),
                    ),
                )
                con.commit()
            finally:
                con.close()
        return self.get_user_quota(uid)

    # --- library moderation reports ---
    def create_library_report(
        self,
        *,
        report_id: str,
        reporter_id: str,
        job_id: str,
        series_slug: str,
        season_number: int,
        episode_number: int,
        reason: str,
        owner_id: str = "",
        notified: bool = False,
        notify_error: str | None = None,
    ) -> dict[str, Any]:
        rid = str(report_id or "").strip()
        reporter = str(reporter_id or "").strip()
        job_id = str(job_id or "").strip()
        slug = str(series_slug or "").strip()
        if not rid or not reporter or not job_id or not slug:
            raise ValueError("report_id, reporter_id, job_id, series_slug required")
        now = float(time.time())
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    INSERT INTO library_reports (
                      id, job_id, series_slug, season_number, episode_number,
                      reporter_id, owner_id, reason, created_at, status, notified, notify_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        rid,
                        job_id,
                        slug,
                        int(season_number),
                        int(episode_number),
                        reporter,
                        str(owner_id or ""),
                        str(reason or ""),
                        now,
                        "open",
                        1 if notified else 0,
                        str(notify_error or ""),
                    ),
                )
                con.commit()
            finally:
                con.close()
        return {
            "id": rid,
            "job_id": job_id,
            "series_slug": slug,
            "season_number": int(season_number),
            "episode_number": int(episode_number),
            "reporter_id": reporter,
            "owner_id": str(owner_id or ""),
            "reason": str(reason or ""),
            "created_at": float(now),
            "status": "open",
            "notified": bool(notified),
            "notify_error": str(notify_error or ""),
        }

    def update_report_notification(
        self,
        report_id: str,
        *,
        notified: bool,
        notify_error: str | None = None,
    ) -> None:
        rid = str(report_id or "").strip()
        if not rid:
            return
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    UPDATE library_reports
                    SET notified = ?, notify_error = ?
                    WHERE id = ?;
                    """,
                    (1 if notified else 0, str(notify_error or ""), rid),
                )
                con.commit()
            finally:
                con.close()

    def list_library_reports(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        lim = max(1, min(500, int(limit)))
        off = max(0, int(offset))
        where = ""
        params: list[Any] = []
        if status:
            where = "WHERE status = ?"
            params.append(str(status))
        con = self._conn()
        try:
            rows = con.execute(
                f"""
                SELECT id, job_id, series_slug, season_number, episode_number,
                       reporter_id, owner_id, reason, created_at, status, notified, notify_error
                FROM library_reports
                {where}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?;
                """,
                [*params, lim, off],
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                out.append(
                    {
                        "id": str(r["id"]),
                        "job_id": str(r["job_id"] or ""),
                        "series_slug": str(r["series_slug"] or ""),
                        "season_number": int(r["season_number"] or 0),
                        "episode_number": int(r["episode_number"] or 0),
                        "reporter_id": str(r["reporter_id"] or ""),
                        "owner_id": str(r["owner_id"] or ""),
                        "reason": str(r["reason"] or ""),
                        "created_at": float(r["created_at"] or 0.0),
                        "status": str(r["status"] or "open"),
                        "notified": bool(int(r["notified"] or 0)),
                        "notify_error": str(r["notify_error"] or ""),
                    }
                )
            return out
        finally:
            con.close()

    def count_library_reports(self, *, status: str | None = None) -> int:
        where = ""
        params: list[Any] = []
        if status:
            where = "WHERE status = ?"
            params.append(str(status))
        con = self._conn()
        try:
            row = con.execute(
                f"SELECT COUNT(*) AS cnt FROM library_reports {where};",
                params,
            ).fetchone()
            if row is None:
                return 0
            return int(row["cnt"] or 0)
        finally:
            con.close()

    def update_report_status(self, report_id: str, *, status: str, handled_by: str = "") -> None:
        rid = str(report_id or "").strip()
        if not rid:
            return
        st = str(status or "").strip().lower() or "open"
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    UPDATE library_reports
                    SET status = ?, notify_error = notify_error
                    WHERE id = ?;
                    """,
                    (st, rid),
                )
                con.commit()
            finally:
                con.close()

    # --- persistent character voices ---
    def upsert_character(
        self,
        *,
        series_slug: str,
        character_slug: str,
        display_name: str = "",
        ref_path: str = "",
        created_by: str = "",
    ) -> dict[str, Any]:
        series_slug = str(series_slug or "").strip()
        character_slug = str(character_slug or "").strip()
        if not series_slug or not character_slug:
            raise ValueError("series_slug and character_slug required")
        now = now_utc()
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    INSERT INTO character_voice(
                      series_slug, character_slug, display_name, ref_path, updated_at, created_by
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(series_slug, character_slug) DO UPDATE SET
                      display_name=excluded.display_name,
                      ref_path=excluded.ref_path,
                      updated_at=excluded.updated_at,
                      created_by=COALESCE(NULLIF(excluded.created_by,''), character_voice.created_by)
                    ;
                    """,
                    (
                        series_slug,
                        character_slug,
                        str(display_name or ""),
                        str(ref_path or ""),
                        now,
                        str(created_by or ""),
                    ),
                )
                con.commit()
            finally:
                con.close()
        return {
            "series_slug": series_slug,
            "character_slug": character_slug,
            "display_name": str(display_name or ""),
            "ref_path": str(ref_path or ""),
            "updated_at": now,
            "created_by": str(created_by or ""),
        }

    def list_characters_for_series(self, series_slug: str) -> list[dict[str, Any]]:
        series_slug = str(series_slug or "").strip()
        if not series_slug:
            return []
        con = self._conn()
        try:
            rows = con.execute(
                """
                SELECT series_slug, character_slug, display_name, ref_path, updated_at, created_by
                FROM character_voice
                WHERE series_slug = ?
                ORDER BY character_slug ASC
                ;
                """,
                (series_slug,),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                out.append({k: r[k] for k in r.keys()})
            return out
        finally:
            con.close()

    def get_character(self, *, series_slug: str, character_slug: str) -> dict[str, Any] | None:
        series_slug = str(series_slug or "").strip()
        character_slug = str(character_slug or "").strip()
        if not series_slug or not character_slug:
            return None
        con = self._conn()
        try:
            row = con.execute(
                """
                SELECT series_slug, character_slug, display_name, ref_path, updated_at, created_by
                FROM character_voice
                WHERE series_slug = ? AND character_slug = ?
                LIMIT 1
                ;
                """,
                (series_slug, character_slug),
            ).fetchone()
            return {k: row[k] for k in row.keys()} if row is not None else None
        finally:
            con.close()

    def delete_character(self, *, series_slug: str, character_slug: str) -> bool:
        series_slug = str(series_slug or "").strip()
        character_slug = str(character_slug or "").strip()
        if not series_slug or not character_slug:
            return False
        with self._write_lock():
            con = self._conn()
            try:
                cur = con.execute(
                    "DELETE FROM character_voice WHERE series_slug = ? AND character_slug = ?;",
                    (series_slug, character_slug),
                )
                con.commit()
                return bool(cur.rowcount and int(cur.rowcount) > 0)
            finally:
                con.close()

    # --- per-job speaker mappings ---
    def upsert_speaker_mapping(
        self,
        *,
        job_id: str,
        speaker_id: str,
        character_slug: str,
        confidence: float = 1.0,
        locked: bool = True,
        created_by: str = "",
    ) -> dict[str, Any]:
        job_id = str(job_id or "").strip()
        speaker_id = str(speaker_id or "").strip()
        character_slug = str(character_slug or "").strip()
        if not job_id or not speaker_id or not character_slug:
            raise ValueError("job_id, speaker_id, character_slug required")
        now = now_utc()
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    INSERT INTO speaker_mapping(
                      job_id, speaker_id, character_slug, confidence, locked, updated_at, created_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id, speaker_id) DO UPDATE SET
                      character_slug=excluded.character_slug,
                      confidence=excluded.confidence,
                      locked=excluded.locked,
                      updated_at=excluded.updated_at,
                      created_by=COALESCE(NULLIF(excluded.created_by,''), speaker_mapping.created_by)
                    ;
                    """,
                    (
                        job_id,
                        speaker_id,
                        character_slug,
                        float(confidence),
                        1 if bool(locked) else 0,
                        now,
                        str(created_by or ""),
                    ),
                )
                con.commit()
            finally:
                con.close()
        return {
            "job_id": job_id,
            "speaker_id": speaker_id,
            "character_slug": character_slug,
            "confidence": float(confidence),
            "locked": bool(locked),
            "updated_at": now,
            "created_by": str(created_by or ""),
        }

    def list_speaker_mappings(self, job_id: str) -> list[dict[str, Any]]:
        job_id = str(job_id or "").strip()
        if not job_id:
            return []
        con = self._conn()
        try:
            rows = con.execute(
                """
                SELECT job_id, speaker_id, character_slug, confidence, locked, updated_at, created_by
                FROM speaker_mapping
                WHERE job_id = ?
                ORDER BY speaker_id ASC
                ;
                """,
                (job_id,),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                d = {k: r[k] for k in r.keys()}
                with suppress(Exception):
                    d["locked"] = bool(int(d.get("locked") or 0))
                with suppress(Exception):
                    d["confidence"] = float(d.get("confidence") or 0.0)
                out.append(d)
            return out
        finally:
            con.close()

    # --- QA review entries ---
    def _qa_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        out = {k: row[k] for k in row.keys()}
        if "segment_id" in out and out["segment_id"] is not None:
            with suppress(Exception):
                out["segment_id"] = int(out["segment_id"])
        for key in ("created_at", "updated_at"):
            if key in out and out[key] is not None:
                with suppress(Exception):
                    out[key] = float(out[key])
        for key in ("pronunciation_overrides", "glossary_used"):
            if key in out and isinstance(out[key], str):
                try:
                    out[key] = json.loads(out[key])
                except Exception:
                    pass
        return out

    def get_qa_review(self, *, job_id: str, segment_id: int) -> dict[str, Any] | None:
        jid = str(job_id or "").strip()
        if not jid:
            return None
        try:
            sid = int(segment_id)
        except Exception:
            return None
        con = self._conn()
        try:
            row = con.execute(
                "SELECT * FROM qa_reviews WHERE job_id = ? AND segment_id = ? LIMIT 1;",
                (jid, int(sid)),
            ).fetchone()
            if row is None:
                return None
            return self._qa_row_to_dict(row)
        finally:
            con.close()

    def list_qa_reviews(self, *, job_id: str) -> list[dict[str, Any]]:
        jid = str(job_id or "").strip()
        if not jid:
            return []
        con = self._conn()
        try:
            rows = con.execute(
                "SELECT * FROM qa_reviews WHERE job_id = ? ORDER BY segment_id ASC;",
                (jid,),
            ).fetchall()
            return [self._qa_row_to_dict(r) for r in rows]
        finally:
            con.close()

    def upsert_qa_review(
        self,
        *,
        job_id: str,
        segment_id: int,
        status: str | None = None,
        notes: str | None = None,
        edited_text: str | None = None,
        pronunciation_overrides: dict[str, Any] | str | None = None,
        glossary_used: dict[str, Any] | str | None = None,
        created_by: str | None = None,
        created_at: float | None = None,
        updated_at: float | None = None,
    ) -> dict[str, Any]:
        jid = str(job_id or "").strip()
        if not jid:
            raise ValueError("job_id is required")
        try:
            sid = int(segment_id)
            if sid <= 0:
                raise ValueError("segment_id must be positive")
        except Exception as ex:
            raise ValueError("segment_id must be an integer") from ex

        existing = self.get_qa_review(job_id=jid, segment_id=sid) or {}
        rec: dict[str, Any] = {}
        rec["id"] = str(existing.get("id") or f"{jid}:{sid}")
        rec["job_id"] = jid
        rec["segment_id"] = int(sid)
        rec["status"] = str(status or existing.get("status") or "pending")
        rec["notes"] = (
            str(notes)
            if notes is not None
            else (str(existing.get("notes")) if existing.get("notes") is not None else None)
        )
        rec["edited_text"] = (
            str(edited_text)
            if edited_text is not None
            else (
                str(existing.get("edited_text"))
                if existing.get("edited_text") is not None
                else None
            )
        )
        rec["created_by"] = str(existing.get("created_by") or (created_by or ""))
        rec["created_at"] = float(existing.get("created_at") or created_at or time.time())
        rec["updated_at"] = float(updated_at or time.time())

        def _json_dump(val: dict[str, Any] | str | None) -> str | None:
            if val is None:
                return None
            if isinstance(val, str):
                return val
            try:
                return json.dumps(val, sort_keys=True)
            except Exception:
                return json.dumps({})

        if pronunciation_overrides is not None:
            rec["pronunciation_overrides"] = _json_dump(pronunciation_overrides)
        else:
            rec["pronunciation_overrides"] = (
                _json_dump(existing.get("pronunciation_overrides"))
                if existing.get("pronunciation_overrides") is not None
                else None
            )
        if glossary_used is not None:
            rec["glossary_used"] = _json_dump(glossary_used)
        else:
            rec["glossary_used"] = (
                _json_dump(existing.get("glossary_used"))
                if existing.get("glossary_used") is not None
                else None
            )

        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    INSERT INTO qa_reviews (
                      id, job_id, segment_id, status, notes, edited_text,
                      pronunciation_overrides, glossary_used, created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      status=excluded.status,
                      notes=excluded.notes,
                      edited_text=excluded.edited_text,
                      pronunciation_overrides=excluded.pronunciation_overrides,
                      glossary_used=excluded.glossary_used,
                      updated_at=excluded.updated_at,
                      created_by=qa_reviews.created_by,
                      created_at=qa_reviews.created_at;
                    """,
                    (
                        rec["id"],
                        rec["job_id"],
                        rec["segment_id"],
                        rec["status"],
                        rec["notes"],
                        rec["edited_text"],
                        rec["pronunciation_overrides"],
                        rec["glossary_used"],
                        rec["created_by"],
                        rec["created_at"],
                        rec["updated_at"],
                    ),
                )
                con.commit()
            finally:
                con.close()
        return rec

    # --- deterministic glossaries ---
    def list_glossaries(
        self,
        *,
        language_pair: str | None = None,
        series_slug: str | None = None,
        enabled_only: bool = True,
    ) -> list[dict[str, Any]]:
        lp = str(language_pair or "").strip().lower()
        series = str(series_slug or "").strip()
        con = self._conn()
        try:
            where = []
            params: list[Any] = []
            if lp:
                where.append("language_pair = ?")
                params.append(lp)
            if series:
                where.append("series_slug = ?")
                params.append(series)
            if enabled_only:
                where.append("enabled = 1")
            clause = ("WHERE " + " AND ".join(where)) if where else ""
            rows = con.execute(
                f"""
                SELECT id, name, language_pair, series_slug, priority, enabled, rules_json, created_at, updated_at
                FROM glossaries
                {clause}
                ORDER BY priority DESC, name ASC, id ASC;
                """,
                tuple(params),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                d = {k: r[k] for k in r.keys()}
                with suppress(Exception):
                    d["priority"] = int(d.get("priority") or 0)
                with suppress(Exception):
                    d["enabled"] = bool(int(d.get("enabled") or 0))
                with suppress(Exception):
                    d["created_at"] = float(d.get("created_at") or 0)
                with suppress(Exception):
                    d["updated_at"] = float(d.get("updated_at") or 0)
                if isinstance(d.get("rules_json"), str):
                    try:
                        d["rules_json"] = json.loads(d["rules_json"])
                    except Exception:
                        pass
                out.append(d)
            return out
        finally:
            con.close()

    def get_glossary(self, glossary_id: str) -> dict[str, Any] | None:
        gid = str(glossary_id or "").strip()
        if not gid:
            return None
        con = self._conn()
        try:
            row = con.execute(
                """
                SELECT id, name, language_pair, series_slug, priority, enabled, rules_json, created_at, updated_at
                FROM glossaries
                WHERE id = ?
                LIMIT 1;
                """,
                (gid,),
            ).fetchone()
            if row is None:
                return None
            d = {k: row[k] for k in row.keys()}
            with suppress(Exception):
                d["priority"] = int(d.get("priority") or 0)
            with suppress(Exception):
                d["enabled"] = bool(int(d.get("enabled") or 0))
            if isinstance(d.get("rules_json"), str):
                with suppress(Exception):
                    d["rules_json"] = json.loads(d["rules_json"])
            return d
        finally:
            con.close()

    def upsert_glossary(
        self,
        *,
        glossary_id: str,
        name: str,
        language_pair: str,
        rules_json: dict[str, Any] | str,
        series_slug: str | None = None,
        priority: int = 0,
        enabled: bool = True,
    ) -> dict[str, Any]:
        gid = str(glossary_id or "").strip()
        if not gid:
            raise ValueError("glossary_id is required")
        name = str(name or "").strip()
        if not name:
            raise ValueError("name is required")
        lp = str(language_pair or "").strip().lower()
        if not lp or "->" not in lp:
            raise ValueError("language_pair must be like 'ja->en'")
        series = str(series_slug or "").strip() or None
        now = float(time.time())
        if isinstance(rules_json, str):
            rules_raw = rules_json
        else:
            rules_raw = json.dumps(rules_json, sort_keys=True)
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    INSERT INTO glossaries (
                      id, name, language_pair, series_slug, priority, enabled, rules_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      name=excluded.name,
                      language_pair=excluded.language_pair,
                      series_slug=excluded.series_slug,
                      priority=excluded.priority,
                      enabled=excluded.enabled,
                      rules_json=excluded.rules_json,
                      updated_at=excluded.updated_at;
                    """,
                    (
                        gid,
                        name,
                        lp,
                        series,
                        int(priority),
                        1 if bool(enabled) else 0,
                        rules_raw,
                        now,
                        now,
                    ),
                )
                con.commit()
            finally:
                con.close()
        return {
            "id": gid,
            "name": name,
            "language_pair": lp,
            "series_slug": series,
            "priority": int(priority),
            "enabled": bool(enabled),
            "rules_json": json.loads(rules_raw) if isinstance(rules_raw, str) else rules_raw,
        }

    def delete_glossary(self, glossary_id: str) -> bool:
        gid = str(glossary_id or "").strip()
        if not gid:
            return False
        with self._write_lock():
            con = self._conn()
            try:
                cur = con.execute("DELETE FROM glossaries WHERE id = ?;", (gid,))
                con.commit()
                return bool(cur.rowcount)
            finally:
                con.close()

    # --- pronunciation dictionary ---
    def list_pronunciations(self, *, lang: str | None = None, term: str | None = None) -> list[dict]:
        lang_q = str(lang or "").strip().lower()
        term_q = str(term or "").strip()
        con = self._conn()
        try:
            where = []
            params: list[Any] = []
            if lang_q:
                where.append("lang = ?")
                params.append(lang_q)
            if term_q:
                where.append("term = ?")
                params.append(term_q)
            clause = ("WHERE " + " AND ".join(where)) if where else ""
            rows = con.execute(
                f"""
                SELECT id, lang, term, ipa_or_phoneme, example, created_by, created_at
                FROM pronunciation_dict
                {clause}
                ORDER BY term ASC, id ASC;
                """,
                tuple(params),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                d = {k: r[k] for k in r.keys()}
                with suppress(Exception):
                    d["created_at"] = float(d.get("created_at") or 0)
                if isinstance(d.get("ipa_or_phoneme"), str):
                    with suppress(Exception):
                        d["ipa_or_phoneme"] = json.loads(d["ipa_or_phoneme"])
                out.append(d)
            return out
        finally:
            con.close()

    def upsert_pronunciation(
        self,
        *,
        entry_id: str,
        lang: str,
        term: str,
        ipa_or_phoneme: dict[str, Any] | str,
        example: str | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        eid = str(entry_id or "").strip()
        if not eid:
            raise ValueError("entry_id is required")
        lang_n = str(lang or "").strip().lower()
        term_n = str(term or "").strip()
        if not lang_n or not term_n:
            raise ValueError("lang and term are required")
        raw = ipa_or_phoneme if isinstance(ipa_or_phoneme, str) else json.dumps(
            ipa_or_phoneme, sort_keys=True
        )
        now = float(time.time())
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    INSERT INTO pronunciation_dict (
                      id, lang, term, ipa_or_phoneme, example, created_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      lang=excluded.lang,
                      term=excluded.term,
                      ipa_or_phoneme=excluded.ipa_or_phoneme,
                      example=excluded.example,
                      created_by=pronunciation_dict.created_by,
                      created_at=pronunciation_dict.created_at;
                    """,
                    (
                        eid,
                        lang_n,
                        term_n,
                        raw,
                        str(example or "") if example is not None else None,
                        str(created_by or ""),
                        now,
                    ),
                )
                con.commit()
            finally:
                con.close()
        out: dict[str, Any] = {
            "id": eid,
            "lang": lang_n,
            "term": term_n,
            "ipa_or_phoneme": ipa_or_phoneme,
            "example": str(example or "") if example is not None else None,
            "created_by": str(created_by or ""),
            "created_at": now,
        }
        return out

    def delete_pronunciation(self, entry_id: str) -> bool:
        eid = str(entry_id or "").strip()
        if not eid:
            return False
        with self._write_lock():
            con = self._conn()
            try:
                cur = con.execute("DELETE FROM pronunciation_dict WHERE id = ?;", (eid,))
                con.commit()
                return bool(cur.rowcount)
            finally:
                con.close()

    # --- voice profiles ---
    def _parse_embedding_vector(self, raw: object) -> list[float] | None:
        if raw is None:
            return None
        data = raw
        if isinstance(data, (bytes, bytearray)):
            try:
                data = data.decode("utf-8", errors="ignore")
            except Exception:
                return None
        if isinstance(data, list):
            try:
                return [float(x) for x in data]
            except Exception:
                return None
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                if isinstance(parsed, list):
                    return [float(x) for x in parsed]
            except Exception:
                return None
        return None

    def _parse_metadata_json(self, raw: object) -> dict[str, Any] | None:
        if raw is None:
            return None
        data = raw
        if isinstance(data, (bytes, bytearray)):
            try:
                data = data.decode("utf-8", errors="ignore")
            except Exception:
                return None
        if isinstance(data, dict):
            return data
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return None
        return None

    def get_voice_profile(self, profile_id: str) -> dict[str, Any] | None:
        pid = str(profile_id or "").strip()
        if not pid:
            return None
        con = self._conn()
        try:
            row = con.execute("SELECT * FROM voice_profiles WHERE id = ? LIMIT 1;", (pid,)).fetchone()
            if row is None:
                return None
            d = {k: row[k] for k in row.keys()}
            d["embedding_vector"] = self._parse_embedding_vector(d.get("embedding_vector"))
            d["metadata_json"] = self._parse_metadata_json(d.get("metadata_json"))
            return d
        finally:
            con.close()

    def list_voice_profiles(
        self, *, series_slug: str | None = None, allow_global: bool = False
    ) -> list[dict[str, Any]]:
        series = str(series_slug or "").strip()
        con = self._conn()
        try:
            rows = con.execute("SELECT * FROM voice_profiles;", ()).fetchall()
            out: list[dict[str, Any]] = []
            now = float(time.time())
            for r in rows:
                d = {k: r[k] for k in r.keys()}
                series_lock = str(d.get("series_lock") or "").strip()
                scope = str(d.get("scope") or "private").strip().lower()
                exp = d.get("expires_at")
                try:
                    if exp is not None and float(exp) > 0 and float(exp) < now:
                        continue
                except Exception:
                    pass
                if series:
                    if series_lock == series:
                        pass
                    elif allow_global and not series_lock and scope in {"global", "friends"}:
                        pass
                    else:
                        continue
                d["embedding_vector"] = self._parse_embedding_vector(d.get("embedding_vector"))
                d["metadata_json"] = self._parse_metadata_json(d.get("metadata_json"))
                out.append(d)
            return out
        finally:
            con.close()

    def upsert_voice_profile(
        self,
        *,
        profile_id: str,
        display_name: str,
        created_by: str,
        scope: str,
        series_lock: str | None,
        source_type: str,
        export_allowed: bool,
        share_allowed: bool,
        reuse_allowed: int | None,
        expires_at: float | None,
        embedding_vector: list[float] | None,
        embedding_model_id: str | None,
        metadata_json: dict[str, Any] | str | None,
    ) -> dict[str, Any]:
        pid = str(profile_id or "").strip()
        if not pid:
            raise ValueError("profile_id required")
        existing = self.get_voice_profile(pid) or {}
        created_at = float(existing.get("created_at") or time.time())
        created_by_eff = str(existing.get("created_by") or created_by or "")
        meta = metadata_json
        if isinstance(meta, dict):
            meta_raw: str | None = json.dumps(meta, sort_keys=True)
        elif isinstance(meta, str):
            meta_raw = meta
        else:
            meta_raw = None
        emb_raw = None
        if embedding_vector is not None:
            emb_raw = json.dumps([float(x) for x in embedding_vector])
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    INSERT INTO voice_profiles (
                      id, display_name, created_by, created_at, scope, series_lock,
                      source_type, export_allowed, share_allowed, reuse_allowed,
                      expires_at, embedding_vector, embedding_model_id, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      display_name=excluded.display_name,
                      scope=excluded.scope,
                      series_lock=excluded.series_lock,
                      source_type=excluded.source_type,
                      export_allowed=excluded.export_allowed,
                      share_allowed=excluded.share_allowed,
                      reuse_allowed=excluded.reuse_allowed,
                      expires_at=excluded.expires_at,
                      embedding_vector=excluded.embedding_vector,
                      embedding_model_id=excluded.embedding_model_id,
                      metadata_json=excluded.metadata_json;
                    """,
                    (
                        pid,
                        str(display_name or ""),
                        created_by_eff,
                        created_at,
                        str(scope or "private"),
                        str(series_lock or "") or None,
                        str(source_type or "unknown"),
                        1 if bool(export_allowed) else 0,
                        1 if bool(share_allowed) else 0,
                        int(reuse_allowed) if reuse_allowed is not None else None,
                        float(expires_at) if expires_at is not None else None,
                        emb_raw,
                        str(embedding_model_id or ""),
                        meta_raw,
                    ),
                )
                con.commit()
            finally:
                con.close()
        return {
            "id": pid,
            "display_name": str(display_name or ""),
            "created_by": created_by_eff,
            "created_at": created_at,
            "scope": str(scope or "private"),
            "series_lock": str(series_lock or "") or None,
            "source_type": str(source_type or "unknown"),
            "export_allowed": bool(export_allowed),
            "share_allowed": bool(share_allowed),
            "reuse_allowed": reuse_allowed,
            "expires_at": expires_at,
            "embedding_vector": embedding_vector,
            "embedding_model_id": str(embedding_model_id or ""),
            "metadata_json": self._parse_metadata_json(meta_raw),
        }

    def list_voice_profile_suggestions(
        self, profile_id: str, *, status: str | None = "pending"
    ) -> list[dict[str, Any]]:
        pid = str(profile_id or "").strip()
        if not pid:
            return []
        con = self._conn()
        try:
            if status is None:
                rows = con.execute(
                    "SELECT * FROM voice_profile_suggestions WHERE voice_profile_id = ?;",
                    (pid,),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM voice_profile_suggestions WHERE voice_profile_id = ? AND status = ?;",
                    (pid, str(status)),
                ).fetchall()
            out = []
            for r in rows:
                out.append({k: r[k] for k in r.keys()})
            out.sort(key=lambda x: float(x.get("similarity") or 0.0), reverse=True)
            return out
        finally:
            con.close()

    def list_voice_profile_suggestions_all(
        self, *, status: str | None = "pending", limit: int = 200, offset: int = 0
    ) -> list[dict[str, Any]]:
        lim = max(1, min(1000, int(limit)))
        off = max(0, int(offset))
        con = self._conn()
        try:
            if status is None:
                rows = con.execute(
                    "SELECT * FROM voice_profile_suggestions ORDER BY created_at DESC LIMIT ? OFFSET ?;",
                    (lim, off),
                ).fetchall()
            else:
                rows = con.execute(
                    """
                    SELECT * FROM voice_profile_suggestions
                    WHERE status = ?
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?;
                    """,
                    (str(status), lim, off),
                ).fetchall()
            return [{k: r[k] for k in r.keys()} for r in rows]
        finally:
            con.close()

    def get_voice_profile_suggestion(self, suggestion_id: str) -> dict[str, Any] | None:
        sid = str(suggestion_id or "").strip()
        if not sid:
            return None
        con = self._conn()
        try:
            row = con.execute(
                "SELECT * FROM voice_profile_suggestions WHERE id = ? LIMIT 1;", (sid,)
            ).fetchone()
            if row is None:
                return None
            return {k: row[k] for k in row.keys()}
        finally:
            con.close()

    def insert_voice_profile_suggestion(
        self,
        *,
        voice_profile_id: str,
        suggested_profile_id: str,
        similarity: float,
        created_by: str,
        status: str = "pending",
    ) -> dict[str, Any] | None:
        vp = str(voice_profile_id or "").strip()
        sp = str(suggested_profile_id or "").strip()
        if not vp or not sp or vp == sp:
            return None
        now = float(time.time())
        sid = f"vps_{__import__('secrets').token_hex(8)}"
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    INSERT OR IGNORE INTO voice_profile_suggestions (
                      id, voice_profile_id, suggested_profile_id, similarity,
                      status, created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        sid,
                        vp,
                        sp,
                        float(similarity),
                        str(status or "pending"),
                        str(created_by or ""),
                        now,
                        now,
                    ),
                )
                con.commit()
            finally:
                con.close()
        return self.get_voice_profile_suggestion(sid)

    def set_voice_profile_suggestion_status(
        self, suggestion_id: str, *, status: str
    ) -> dict[str, Any] | None:
        sid = str(suggestion_id or "").strip()
        if not sid:
            return None
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    "UPDATE voice_profile_suggestions SET status = ?, updated_at = ? WHERE id = ?;",
                    (str(status), float(time.time()), sid),
                )
                con.commit()
            finally:
                con.close()
        return self.get_voice_profile_suggestion(sid)

    def upsert_voice_profile_alias(
        self,
        *,
        voice_profile_id: str,
        alias_of_voice_profile_id: str,
        confidence: float,
        approved_by_admin: bool = False,
        approved_at: float | None = None,
    ) -> dict[str, Any] | None:
        vp = str(voice_profile_id or "").strip()
        ap = str(alias_of_voice_profile_id or "").strip()
        if not vp or not ap or vp == ap:
            return None
        now = float(time.time())
        aid = f"vpa_{__import__('secrets').token_hex(8)}"
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    INSERT OR IGNORE INTO voice_profile_aliases (
                      id, voice_profile_id, alias_of_voice_profile_id,
                      confidence, approved_by_admin, approved_at
                    ) VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    (
                        aid,
                        vp,
                        ap,
                        float(confidence),
                        1 if bool(approved_by_admin) else 0,
                        float(approved_at) if approved_at is not None else None,
                    ),
                )
                con.commit()
            finally:
                con.close()
        # If insert was ignored, attempt to fetch existing alias.
        con = self._conn()
        try:
            row = con.execute(
                """
                SELECT * FROM voice_profile_aliases
                WHERE voice_profile_id = ? AND alias_of_voice_profile_id = ?
                LIMIT 1;
                """,
                (vp, ap),
            ).fetchone()
            if row is None:
                return None
            d = {k: row[k] for k in row.keys()}
        finally:
            con.close()
        return d

    def approve_voice_profile_alias(
        self, voice_profile_id: str, alias_of_voice_profile_id: str
    ) -> dict[str, Any] | None:
        vp = str(voice_profile_id or "").strip()
        ap = str(alias_of_voice_profile_id or "").strip()
        if not vp or not ap:
            return None
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    UPDATE voice_profile_aliases
                    SET approved_by_admin = 1, approved_at = ?
                    WHERE voice_profile_id = ? AND alias_of_voice_profile_id = ?;
                    """,
                    (float(time.time()), vp, ap),
                )
                con.commit()
            finally:
                con.close()
        con = self._conn()
        try:
            row = con.execute(
                """
                SELECT * FROM voice_profile_aliases
                WHERE voice_profile_id = ? AND alias_of_voice_profile_id = ?
                LIMIT 1;
                """,
                (vp, ap),
            ).fetchone()
            if row is None:
                return None
            return {k: row[k] for k in row.keys()}
        finally:
            con.close()

    def has_voice_profile_alias(self, voice_profile_id: str, alias_of_voice_profile_id: str) -> bool:
        vp = str(voice_profile_id or "").strip()
        ap = str(alias_of_voice_profile_id or "").strip()
        if not vp or not ap:
            return False
        con = self._conn()
        try:
            row = con.execute(
                """
                SELECT 1 FROM voice_profile_aliases
                WHERE voice_profile_id = ? AND alias_of_voice_profile_id = ?
                LIMIT 1;
                """,
                (vp, ap),
            ).fetchone()
            return row is not None
        finally:
            con.close()

    def get_voice_profile_alias(
        self, voice_profile_id: str, alias_of_voice_profile_id: str
    ) -> dict[str, Any] | None:
        vp = str(voice_profile_id or "").strip()
        ap = str(alias_of_voice_profile_id or "").strip()
        if not vp or not ap:
            return None
        con = self._conn()
        try:
            row = con.execute(
                """
                SELECT * FROM voice_profile_aliases
                WHERE voice_profile_id = ? AND alias_of_voice_profile_id = ?
                LIMIT 1;
                """,
                (vp, ap),
            ).fetchone()
            if row is None:
                return None
            return {k: row[k] for k in row.keys()}
        finally:
            con.close()

    def _maybe_upsert_library_from_raw(self, job_id: str, raw: dict[str, Any]) -> None:
        """
        Best-effort denormalized index for library browsing.

        This is intentionally tolerant: legacy jobs may not have library fields yet.
        Caller must hold the write lock.
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

        try:
            from dubbing_pipeline.jobs.models import normalize_visibility

            vis = normalize_visibility(str(raw.get("visibility") or "private")).value
        except Exception:
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
        with self._write_lock(), self._lock, self._jobs() as db:
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
        with self._write_lock(), self._lock, self._jobs() as db:
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

    def list_all(self) -> list[Job]:
        with self._lock, self._jobs() as db:
            items = list(db.items())
        jobs = [Job.from_dict(v) for _, v in items]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    def delete_job(self, id: str) -> None:
        if not id:
            return
        with self._write_lock():
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
        with suppress(Exception):
            self.delete_job_storage(str(id))

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
        with self._write_lock(), self._lock, self._idem() as db:
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
        with self._write_lock(), self._lock, self._presets() as db:
            db[pid] = dict(preset)
        return dict(preset)

    def delete_preset(self, preset_id: str) -> None:
        with self._write_lock(), self._lock, self._presets() as db, suppress(Exception):
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
        with self._write_lock(), self._lock, self._projects() as db:
            db[pid] = dict(project)
        return dict(project)

    def delete_project(self, project_id: str) -> None:
        with self._write_lock(), self._lock, self._projects() as db, suppress(Exception):
            del db[str(project_id)]

    # --- resumable uploads (web/mobile) ---
    def put_upload(self, upload_id: str, rec: dict[str, Any]) -> dict[str, Any]:
        if not upload_id:
            raise ValueError("upload_id required")
        with self._write_lock(), self._lock, self._uploads() as db:
            db[str(upload_id)] = dict(rec)
            return dict(rec)

    def get_upload(self, upload_id: str) -> dict[str, Any] | None:
        if not upload_id:
            return None
        with self._lock, self._uploads() as db:
            v = db.get(str(upload_id))
        return dict(v) if isinstance(v, dict) else None

    def list_uploads(self) -> list[dict[str, Any]]:
        with self._lock, self._uploads() as db:
            items = list(db.items())
        out: list[dict[str, Any]] = []
        for _, rec in items:
            if isinstance(rec, dict):
                out.append(dict(rec))
        return out

    def update_upload(self, upload_id: str, **fields: Any) -> dict[str, Any] | None:
        if not upload_id:
            return None
        with self._write_lock(), self._lock, self._uploads() as db:
            raw = db.get(str(upload_id))
            if not isinstance(raw, dict):
                return None
            raw = dict(raw)
            raw.update(fields)
            db[str(upload_id)] = raw
            return dict(raw)

    def delete_upload(self, upload_id: str) -> None:
        with self._write_lock(), self._lock, self._uploads() as db, suppress(Exception):
            del db[str(upload_id)]
        with suppress(Exception):
            self.delete_upload_storage(str(upload_id))
