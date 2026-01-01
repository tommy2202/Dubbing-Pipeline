from __future__ import annotations

import subprocess
from pathlib import Path

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


def run_ffmpeg(argv: list[str], *, timeout_s: int | None = None) -> None:
    _validate_args(argv)
    try:
        subprocess.run(
            argv,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as ex:
        raise FFmpegError(f"ffmpeg timed out after {timeout_s}s") from ex
    except Exception as ex:
        raise FFmpegError(f"ffmpeg failed: {ex}") from ex


def ffprobe_duration_seconds(path: Path, *, timeout_s: int = 20) -> float:
    argv = [
        "ffprobe",
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
) -> None:
    argv = ["ffmpeg", "-y"]
    if start_s is not None:
        argv += ["-ss", f"{float(start_s):.3f}"]
    if end_s is not None:
        argv += ["-to", f"{float(end_s):.3f}"]
    argv += ["-i", str(src), "-ac", "1", "-ar", "16000", str(dst)]
    run_ffmpeg(argv, timeout_s=timeout_s)
