from __future__ import annotations

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, TextIO


class FileLockTimeout(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FileLockConfig:
    timeout_s: float = 30.0
    poll_interval_s: float = 0.1


def _acquire_posix(fp: TextIO, *, timeout_s: float, poll_interval_s: float) -> None:
    import fcntl  # noqa: WPS433 - stdlib on posix

    deadline = time.monotonic() + float(timeout_s)
    while True:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise FileLockTimeout("Timed out acquiring file lock") from None
            time.sleep(float(poll_interval_s))


def _release_posix(fp: TextIO) -> None:
    import fcntl  # noqa: WPS433 - stdlib on posix

    with __import__("contextlib").suppress(Exception):
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


def _acquire_windows(fp: TextIO, *, timeout_s: float, poll_interval_s: float) -> None:
    import msvcrt  # type: ignore

    deadline = time.monotonic() + float(timeout_s)
    while True:
        try:
            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise FileLockTimeout("Timed out acquiring file lock") from None
            time.sleep(float(poll_interval_s))


def _release_windows(fp: TextIO) -> None:
    import msvcrt  # type: ignore

    with __import__("contextlib").suppress(Exception):
        msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def file_lock(path: Path, *, timeout_s: float = 30.0, poll_interval_s: float = 0.1) -> Iterator[None]:
    """
    Cross-process file lock using stdlib primitives.
    """
    lock_path = Path(path).resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fp = lock_path.open("a+", encoding="utf-8")
    try:
        if os.name == "nt":
            _acquire_windows(fp, timeout_s=timeout_s, poll_interval_s=poll_interval_s)
        else:
            _acquire_posix(fp, timeout_s=timeout_s, poll_interval_s=poll_interval_s)
        yield
    finally:
        if os.name == "nt":
            _release_windows(fp)
        else:
            _release_posix(fp)
        with __import__("contextlib").suppress(Exception):
            fp.close()
