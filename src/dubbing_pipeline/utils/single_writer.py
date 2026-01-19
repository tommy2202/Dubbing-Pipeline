from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.log import logger


class SingleWriterError(RuntimeError):
    pass


def _env_flag(name: str) -> bool:
    import os

    val = str(os.environ.get(name) or "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def is_single_writer_enabled() -> bool:
    try:
        return bool(getattr(get_settings(), "single_writer_mode", False))
    except Exception:
        return _env_flag("SINGLE_WRITER_MODE")


def single_writer_role() -> str:
    import os

    try:
        role = str(getattr(get_settings(), "single_writer_role", "") or "")
    except Exception:
        role = ""
    role = role or str(os.environ.get("SINGLE_WRITER_ROLE") or "")
    return (role or "writer").strip().lower()


def is_writer() -> bool:
    return single_writer_role() in {"writer", "primary"}


def _lock_path() -> Path:
    import os

    try:
        val = getattr(get_settings(), "single_writer_lock_path", None)
        if val:
            return Path(val).resolve()
    except Exception:
        val = None
    env_val = str(os.environ.get("SINGLE_WRITER_LOCK_PATH") or "").strip()
    if env_val:
        return Path(env_val).resolve()
    try:
        from dubbing_pipeline.config import get_settings as _gs

        s = _gs()
        base = Path(getattr(s, "output_dir", Path.cwd() / "Output")).resolve()
    except Exception:
        base = Path.cwd()
    return (base / "_state" / "metadata.lock").resolve()


def ensure_write_allowed(op: str) -> None:
    if not is_single_writer_enabled():
        return
    if not is_writer():
        logger.warning("single_writer_write_blocked", op=str(op), role=single_writer_role())
        raise SingleWriterError(f"single-writer mode: write blocked ({op})")


@contextmanager
def writer_lock(op: str) -> Iterator[None]:
    if not is_single_writer_enabled():
        yield
        return
    ensure_write_allowed(op)
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl  # type: ignore
    except Exception:
        # If fcntl isn't available, fall back to no-op locking.
        yield
        return
    with open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
