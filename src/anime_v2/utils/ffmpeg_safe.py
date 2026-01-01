from __future__ import annotations

import subprocess
from contextlib import suppress
from pathlib import Path

from anime_v2.config import get_settings

_FORBIDDEN_FLAGS = {
    "-filter_script",
    "-filter_script:v",
    "-filter_script:a",
    "-stats_file",
}


class FFmpegError(RuntimeError):
    pass


def _validate_args(argv: list[str]) -> None:
    for a in argv:
        if a in _FORBIDDEN_FLAGS:
            raise FFmpegError(f"Forbidden ffmpeg/ffprobe flag: {a}")


def _tail(s: str, n: int = 4000) -> str:
    s = str(s or "")
    if len(s) <= n:
        return s
    return s[-n:]


def run_ffmpeg(
    argv: list[str],
    *,
    timeout_s: int | None = None,
    retries: int = 0,
    capture: bool = False,
) -> subprocess.CompletedProcess[str] | None:
    _validate_args(argv)
    last_ex: Exception | None = None
    for attempt in range(int(retries) + 1):
        try:
            if capture:
                return subprocess.run(
                    argv,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                )
            subprocess.run(
                argv,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_s,
            )
            return None
        except subprocess.TimeoutExpired as ex:
            last_ex = ex
            if attempt >= int(retries):
                raise FFmpegError(f"ffmpeg timed out after {timeout_s}s") from ex
        except subprocess.CalledProcessError as ex:
            last_ex = ex
            if attempt >= int(retries):
                stderr = ""
                with suppress(Exception):
                    stderr = ex.stderr.decode("utf-8", errors="replace") if ex.stderr else ""
                raise FFmpegError(
                    "ffmpeg failed "
                    f"(exit={ex.returncode})\n"
                    f"argv={argv}\n"
                    f"stderr_tail={_tail(stderr)}"
                ) from ex
        except Exception as ex:
            last_ex = ex
            if attempt >= int(retries):
                raise FFmpegError(f"ffmpeg failed: {ex} (argv={argv})") from ex
    if last_ex:
        raise FFmpegError(f"ffmpeg failed: {last_ex} (argv={argv})") from last_ex
    raise FFmpegError(f"ffmpeg failed (argv={argv})")


def ffprobe_duration_seconds(path: Path, *, timeout_s: int = 20) -> float:
    s = get_settings()
    argv = [
        str(s.ffprobe_bin),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    _validate_args(argv)
    try:
        out = (
            subprocess.check_output(argv, stderr=subprocess.DEVNULL, timeout=timeout_s)
            .decode("utf-8", errors="replace")
            .strip()
        )
        return float(out)
    except subprocess.TimeoutExpired as ex:
        raise FFmpegError("ffprobe timed out") from ex
    except Exception as ex:
        raise FFmpegError(f"ffprobe failed: {ex}") from ex


def extract_audio_mono_16k(
    *,
    src: Path,
    dst: Path,
    start_s: float | None = None,
    end_s: float | None = None,
    timeout_s: int = 120,
    retries: int = 0,
) -> None:
    s = get_settings()
    argv = [str(s.ffmpeg_bin), "-y"]
    if start_s is not None:
        argv += ["-ss", f"{float(start_s):.3f}"]
    if end_s is not None:
        argv += ["-to", f"{float(end_s):.3f}"]
    argv += ["-i", str(src), "-ac", "1", "-ar", "16000", str(dst)]
    run_ffmpeg(argv, timeout_s=timeout_s, retries=int(retries))
