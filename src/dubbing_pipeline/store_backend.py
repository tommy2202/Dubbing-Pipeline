from __future__ import annotations

from pathlib import Path

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.store import JobStore
from dubbing_pipeline.utils.log import logger


class LocalStore(JobStore):
    """
    Local SQLite-backed store (default).
    """


class PostgresStore:
    """
    Placeholder for a Postgres-backed store.
    """

    def __init__(self, dsn: str) -> None:
        raise RuntimeError("PostgresStore is not implemented in this release")


def build_store(db_path: Path) -> JobStore:
    """
    Build the job store based on STORE_BACKEND.

    Falls back to LocalStore if Postgres is not configured or unavailable.
    """
    s = get_settings()
    backend = str(getattr(s, "store_backend", "local") or "local").strip().lower()
    if backend == "postgres":
        dsn = str(getattr(s, "postgres_dsn", "") or "").strip()
        if not dsn:
            logger.warning("store_backend_postgres_missing_dsn")
            return LocalStore(db_path)
        try:
            import psycopg  # type: ignore  # noqa: F401
        except Exception as ex:
            logger.warning("store_backend_postgres_unavailable", error=str(ex))
            return LocalStore(db_path)
        logger.warning("store_backend_postgres_not_implemented")
        return LocalStore(db_path)
    if backend and backend != "local":
        logger.warning("store_backend_invalid", value=str(backend))
    return LocalStore(db_path)
